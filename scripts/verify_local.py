"""Start an isolated temporary server and run the live demo (optionally browser checks)."""
import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="kena-smoke-") as temporary:
        env = {**os.environ, "KENA_DB": str(Path(temporary) / "smoke.db"), "KENA_TEST_URL": url}
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Local verification server exited")
                    try:
                        client.get(url + "/healthz").raise_for_status()
                        break
                    except (httpx.HTTPError, OSError):
                        time.sleep(.1)
                else:
                    raise RuntimeError("Local verification server did not become ready")
            subprocess.run([sys.executable, "scripts/demo.py", "--base-url", url], cwd=root, env=env, check=True)
            if args.browser:
                subprocess.run(["node", "scripts/browser-test.cjs"], cwd=root, env=env, check=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
