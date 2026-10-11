"""Optional integration with the published shared contract, not a package fake."""

from concurrent.futures import Future
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))
from auroraview_unreal import Client
from owner_dispatch import OwnerDispatcher
import python_client_test as client_tests
from python_client_test import Peer, TOKEN

if importlib.util.find_spec("auroraview_dcc_mcp") is not None:
    from auroraview_dcc_mcp import CleanupError, ClosedError, ContractError, ThreadError, Tool, ToolSet
    from auroraview_unreal.native_tools import NativeToolBinding
else:
    ToolSet = None


@unittest.skipIf(ToolSet is None, "Install the pinned auroraview-unreal[dcc-mcp] extra")
class NativeToolTests(unittest.TestCase):
    def tearDown(self):
        client_tests.PythonClientTests.tearDown(self)

    def tools(self, handler, *, subscribe=None):
        return ToolSet("unreal_test", [Tool(
            "demo.echo", "Echo a boolean on the registered owner thread.",
            {"type": "object", "properties": {"value": {"type": "boolean"}},
             "required": ["value"], "additionalProperties": False},
            handler, output_schema={"type": "boolean"},
            read_only=True, destructive=False, idempotent=True)], subscribe=subscribe)

    def pump_until(self, dispatcher, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            dispatcher.pump()
            time.sleep(0.005)
        self.assertTrue(condition(), "The dispatched operation did not finish")

    def test_native_reverse_dispatch_preserves_contract_and_falsy_results(self):
        owner, calls = threading.get_ident(), []
        def handler(value):
            calls.append(threading.get_ident())
            return value
        tools, dispatcher = self.tools(handler), OwnerDispatcher()
        with Peer() as host, Client(host.port, TOKEN) as client:
            binding = NativeToolBinding(client, tools, dispatch=dispatcher)
            self.assertEqual(host.tools, set())
            self.assertIs(binding.register().register(), binding)
            descriptor = tools.list_tools()[0]
            self.assertEqual(host.registrations, [dict(descriptor, parameters=descriptor["inputSchema"])])
            result = host.reverse("demo.echo", {"value": False})
            self.pump_until(dispatcher, result.done)
            self.assertIs(result.result()["result"], False)
            self.assertEqual(calls, [owner])
            invalid = host.reverse("demo.echo", {"value": 0})
            self.pump_until(dispatcher, invalid.done)
            self.assertEqual(invalid.result()["error"]["name"], "ContractError")
            # Business params cannot redirect the captured method identifier.
            redirected = host.reverse("demo.echo", {"value": True, "_name": "other.tool"})
            self.pump_until(dispatcher, redirected.done)
            self.assertEqual(redirected.result()["error"]["name"], "ContractError")
            self.assertEqual(calls, [owner])
            binding.close()
            binding.close()
            self.assertFalse(tools.closed)
            self.assertIs(tools.call("demo.echo", {"value": True}), True)
            self.assertEqual(client.call("test.echo", 0), 0)
        tools.close()
        dispatcher.close()

    def test_events_use_existing_owner_source_and_single_native_emit(self):
        dispatcher, deliveries, callbacks = OwnerDispatcher(), [], {}
        def subscribe(event, callback):
            callbacks.setdefault(event, []).append(callback)
            return lambda: callbacks[event].remove(callback)
        tools = self.tools(lambda value: value, subscribe=subscribe)
        other = tools.borrow()
        other.subscribe("scene", lambda value: deliveries.append(("other", value, threading.get_ident())))
        with Peer() as host, Client(host.port, TOKEN) as client:
            binding = NativeToolBinding(client, tools, dispatch=dispatcher)
            unsubscribe = binding.subscribe("scene", lambda value: deliveries.append(("native", value, threading.get_ident())))
            pending = []
            thread = threading.Thread(target=lambda: pending.append(dispatcher(
                lambda: [callback({"zero": 0}) for callback in tuple(callbacks["scene"])])))
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            dispatcher.pump()
            self.assertEqual([(kind, value) for kind, value, _ in deliveries],
                             [("other", {"zero": 0}), ("native", {"zero": 0})])
            self.assertTrue(all(thread_id == threading.get_ident() for _, _, thread_id in deliveries))
            binding.emit("scene", {"zero": 0})
            self.assertEqual(host.wait_frame(lambda f: f.get("event") == "scene")["data"], {"zero": 0})
            unsubscribe()
            binding.close()
            self.assertEqual(len(callbacks["scene"]), 1)
            callbacks["scene"][0](False)
            self.assertEqual(deliveries[-1][:2], ("other", False))
            self.assertFalse(tools.closed)
        other.close()
        tools.close()
        dispatcher.close()

    def test_close_cancels_queued_native_call_without_executing_handler(self):
        enqueued, calls = threading.Event(), []
        dispatcher = OwnerDispatcher()
        def dispatch(function):
            future = dispatcher(function)
            enqueued.set()
            return future
        tools = self.tools(lambda value: (calls.append(value), value)[1])
        with Peer() as host, Client(host.port, TOKEN) as client:
            binding = NativeToolBinding(client, tools, dispatch=dispatch).register()
            pending = host.reverse("demo.echo", {"value": True})
            self.assertTrue(enqueued.wait(1))
            binding.close()
            dispatcher.pump()
            self.assertFalse(pending.result(2)["ok"])
            self.assertEqual(calls, [])
            with self.assertRaises(ClosedError):
                binding.call("demo.echo", {"value": True})
        tools.close()
        dispatcher.close()

    def test_dispatch_timeout_cancels_queued_work(self):
        dispatcher, calls = OwnerDispatcher(), []
        tools = self.tools(lambda value: (calls.append(value), value)[1])
        with Peer() as host, Client(host.port, TOKEN) as client:
            binding = NativeToolBinding(client, tools, dispatch=dispatcher, timeout=0.05).register()
            result = host.reverse("demo.echo", {"value": True}).result(1)
            self.assertEqual(result["error"]["name"], "TimeoutError")
            dispatcher.pump()
            self.assertEqual(calls, [])
            binding.close()
        tools.close()
        dispatcher.close()

    def test_handler_timeout_is_preserved_after_owner_dispatch(self):
        dispatcher = OwnerDispatcher()
        def handler(value):
            raise TimeoutError("Native readback timed out with specific evidence")
        tools = self.tools(handler)
        with Peer() as host, Client(host.port, TOKEN) as client:
            binding = NativeToolBinding(client, tools, dispatch=dispatcher).register()
            result = host.reverse("demo.echo", {"value": True})
            self.pump_until(dispatcher, result.done)
            self.assertEqual(result.result()["error"]["name"], "TimeoutError")
            self.assertEqual(result.result()["error"]["message"], "Native readback timed out with specific evidence")
            binding.close()
        tools.close()
        dispatcher.close()

    def test_lifecycle_requires_owner_and_wrong_dispatch_cannot_run_handler(self):
        tools, dispatcher = self.tools(lambda value: value), OwnerDispatcher()
        with Peer() as host, Client(host.port, TOKEN) as client:
            def wrong_dispatch(function):
                result = Future()
                try:
                    result.set_result(function())
                except Exception as error:
                    result.set_exception(error)
                return result
            binding = NativeToolBinding(client, tools, dispatch=wrong_dispatch).register()
            self.assertEqual(host.reverse("demo.echo", {"value": True}).result(2)["error"]["name"], "ThreadError")
            failures = []
            def foreign_close():
                try:
                    binding.close()
                except Exception as error:
                    failures.append(error)
            thread = threading.Thread(target=foreign_close)
            thread.start()
            thread.join(1)
            self.assertIsInstance(failures[0], ThreadError)
            self.assertIs(binding.call("demo.echo", {"value": True}), True)
            binding.close()
        tools.close()
        dispatcher.close()

    def test_cleanup_failures_are_retryable_and_do_not_close_owner(self):
        attempts = []
        def subscribe(_event, _callback):
            def unsubscribe():
                attempts.append(True)
                if len(attempts) == 1:
                    raise RuntimeError("Host unsubscribe failed")
            return unsubscribe
        tools, dispatcher = self.tools(lambda value: value, subscribe=subscribe), OwnerDispatcher()
        with Peer() as host, Client(host.port, TOKEN) as client:
            binding = NativeToolBinding(client, tools, dispatch=dispatcher).register()
            binding.subscribe("scene", lambda value: None)
            with self.assertRaisesRegex(CleanupError, "Host unsubscribe failed"):
                binding.close()
            binding.close()
            self.assertEqual(len(attempts), 2)
            self.assertIs(tools.call("demo.echo", {"value": False}), False)
            self.assertEqual(client.call("test.echo", 0), 0)
        tools.close()
        dispatcher.close()

    def test_native_unregister_failure_revokes_lease_and_retries_owned_handle(self):
        tools, dispatcher = self.tools(lambda value: value), OwnerDispatcher()
        with Peer() as host, Client(host.port, TOKEN) as client:
            binding = NativeToolBinding(client, tools, dispatch=dispatcher).register()
            with mock.patch.object(client, "call", side_effect=TimeoutError("Native unregister unavailable")):
                with self.assertRaisesRegex(CleanupError, "Native unregister unavailable"):
                    binding.close()
            # Native publication may remain until retry, but its consumer lease
            # has already been revoked and cannot execute business code.
            self.assertEqual(host.reverse("demo.echo", {"value": True}).result(2)["error"]["name"], "ClosedError")
            binding.close()
            self.assertNotIn("demo.echo", host.tools)
            self.assertIs(tools.call("demo.echo", {"value": False}), False)
        tools.close()
        dispatcher.close()

    def test_borrowed_core_server_and_other_session_survive_native_close(self):
        if importlib.util.find_spec("dcc_mcp_core") is None:
            self.skipTest("The optional dcc-mcp-core wheel is not installed")
        from dcc_mcp_core.server_base import DccServerBase
        from dcc_mcp_core._server.options import (
            DccServerOptions, ExecutionOptions, GatewayOptions, ObservabilityOptions,
            StandaloneMainThreadExecution)
        tools, dispatcher = self.tools(lambda value: value), OwnerDispatcher()
        with tempfile.TemporaryDirectory() as temporary:
            options = DccServerOptions(
                dcc_name="unreal", builtin_skills_dir=Path(temporary),
                gateway=GatewayOptions(port=0, registry_dir=temporary, enable_failover=False),
                observability=ObservabilityOptions(
                    enable_file_logging=False, enable_job_persistence=False,
                    enable_telemetry=False, enable_checkpoint_persistence=False,
                    enable_checkpoint_tools=False),
                execution=ExecutionOptions(mode=StandaloneMainThreadExecution))
            server = DccServerBase(options)  # Test-owned, never started.
            with mock.patch.object(server, "start") as start, mock.patch.object(server, "stop") as stop:
                agent = tools.attach(server)
                other = tools.borrow()
                with Peer() as host, Client(host.port, TOKEN) as client:
                    binding = NativeToolBinding(client, tools, dispatch=dispatcher).register()
                    binding.close()
                    self.assertIsNotNone(server.get_skill(agent.skill_name))
                    self.assertTrue(server.is_skill_loaded(agent.skill_name))
                    self.assertIs(agent.call("demo.echo", {"value": False}), False)
                    self.assertIs(other.call("demo.echo", {"value": True}), True)
                    self.assertFalse(tools.closed)
                    start.assert_not_called()
                    stop.assert_not_called()
                agent.close()
                # Published Core keeps catalog metadata after unload; executable
                # ownership and the borrowed session token are revoked.
                self.assertFalse(server.is_skill_loaded(agent.skill_name))
                with self.assertRaises(ClosedError):
                    agent.call("demo.echo", {"value": True})
                self.assertIs(other.call("demo.echo", {"value": True}), True)
                stop.assert_not_called()
                other.close()
        tools.close()
        dispatcher.close()


if __name__ == "__main__":
    unittest.main()
