"""Run outside Unreal: python python_tools.py --port 8765 --pid <Unreal PID>.

Set AURORAVIEW_HOST_TOKEN to the native launch token. Launch the native host with
-AuroraViewHostPort=8765 -AuroraViewHostToken=<token> -AuroraViewAllowControl.
Install the SDK with: python -m pip install ./python
"""
import argparse
import json
import os
import threading

from auroraview_unreal import Client


class Tools:
    def add(self, left: float, right: float) -> float:
        """Add two values in the external Python process."""
        return left + right


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--engine", help="Expected Unreal version prefix, such as 5.7")
    parser.add_argument("--context", choices=("editor", "game"))
    parser.add_argument("--editor-python", action="store_true", help="Use optional embedded Editor Python capability")
    options = parser.parse_args()
    with Client(options.port, os.environ["AURORAVIEW_HOST_TOKEN"], expected_pid=options.pid,
                expected_engine=options.engine, expected_context=options.context) as client:
        client.bind_api(Tools(), namespace="python")

        @client.bind_call("python.echo")
        def echo(value):
            """Return a value through native-to-Python Core RPC."""
            return {"echo": value}

        client.on("selection.changed", lambda data: print("Selection event:", data))
        print("Host:", json.dumps(client.identity, indent=2))
        print("Engine:", client.call("unreal.engine.info"))
        worlds = client.call("unreal.world.list")
        print("Worlds:", worlds)
        if worlds:
            print("World object:", client.call("unreal.object.describe", {"object": worlds[0]["object"]}))
        print("Python tool through native:", client.call("python.add", {"left": 2, "right": 3}))
        client.emit("python.tools.ready", {"names": ["python.add", "python.echo"]})
        if options.editor_python:
            print("Editor Python:", client.call("unreal.python.execute", {"code": "import unreal; print(unreal.SystemLibrary.get_engine_version())"}))
        print("Tools registered. Press Ctrl+C to disconnect and unregister owned tools.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
