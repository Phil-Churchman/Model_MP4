"""
Start the local model server.

    venv\\Scripts\\python.exe run_server.py

The control panel opens in a new tab of the default browser once the server is
answering; pass --no-browser to leave it alone.

The control panel is at http://127.0.0.1:8000/app/, or
http://127.0.0.1:8000/animation/animation.html for the existing tools -- the URL space is
the same one Live Server serves, so both work under either.

Binds to 127.0.0.1 only. Live Server binds 0.0.0.0, which puts the Model folder
on every network interface; there is no reason for this one to do the same.
"""

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _port_answers(host, port, timeout=0.25):
    """Whether something is already listening. Used twice, for opposite reasons."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _open_when_ready(url, host, port, timeout=15.0):
    """
    Open the control panel once the server answers.

    Polled rather than opened after a fixed sleep: too short and the browser
    lands on a connection refused, too long and it feels broken. The first
    successful connection is the moment the page will load.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_answers(host, port):
            webbrowser.open_new_tab(url)
            return
        time.sleep(0.1)
    print(f"\nServer did not answer within {timeout:g}s -- open {url} yourself.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1",
                   help="loopback by default; think before widening it")
    p.add_argument("--reload", action="store_true", help="restart on code changes")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open the control panel in a browser")
    args = p.parse_args()

    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "uvicorn is not installed in this interpreter:\n"
            f"  {sys.executable}\n"
            "Install it with:  venv\\Scripts\\python.exe -m pip install fastapi uvicorn")

    from mim.api import app, data_mounts

    print(f"Model folder : {os.path.dirname(os.path.abspath(__file__))}")
    for route, directory in data_mounts().items():
        print(f"Mounted      : {route} -> {directory}")
    print(f"Interpreter  : {sys.executable}")
    # 0.0.0.0 is an address to listen on, not one to visit: browsers do not
    # resolve it usefully. Whatever it binds, reach it over loopback.
    browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    panel_url = f"http://{browser_host}:{args.port}/app/"

    print(f"Control panel: {panel_url}")
    print(f"Animation    : http://{browser_host}:{args.port}/animation/animation.html")

    if args.no_browser:
        pass
    elif _port_answers(browser_host, args.port):
        # Something is already on this port, so uvicorn is about to fail to bind.
        # Opening a tab now would show that other server's page while the
        # terminal reports an error -- the most confusing possible pair.
        print(f"\nPort {args.port} is already in use; not opening a browser.")
    else:
        threading.Thread(target=_open_when_ready,
                         args=(panel_url, browser_host, args.port),
                         daemon=True).start()

    uvicorn.run("mim.api:app" if args.reload else app,
                host=args.host, port=args.port, reload=args.reload,
                log_level="warning")


if __name__ == "__main__":
    main()
