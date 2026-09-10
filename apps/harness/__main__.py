"""Run the fault-injection harness: python -m apps.harness [port]"""

import sys

import uvicorn

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    uvicorn.run("apps.harness.app:app", host="127.0.0.1", port=port, log_level="warning")
