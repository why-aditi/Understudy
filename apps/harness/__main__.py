"""Run the fault-injection harness: python -m apps.harness"""

import sys

from apps.harness.server import serve

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"harness on http://127.0.0.1:{port}/tenant-a/  (ctrl-c to stop)")  # noqa: T201
    serve(port=port)
