"""Computational worker executed only inside its disposable container."""

import json
import resource
import signal
import sys

from runweave_python_policy import SAFE_BUILTINS, validate_program  # type: ignore[import-not-found]


def main() -> None:
    signal.alarm(4)
    resource.setrlimit(resource.RLIMIT_AS, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError("INPUT_LIMIT")
        request = json.loads(raw)
        validate_program(request["code"])
        scope = {"__builtins__": SAFE_BUILTINS, "input_data": request["data"]}
        exec(compile(request["code"], "<sandbox>", "exec"), scope)
        rendered = json.dumps({"ok": True, "result": scope.get("result")}, allow_nan=False)
        if len(rendered.encode()) > 4096:
            raise ValueError("OUTPUT_LIMIT")
    except Exception:
        rendered = json.dumps({"ok": False, "code": "PYTHON_EXECUTION_REJECTED"})
    print(rendered, flush=True)


if __name__ == "__main__":
    main()
