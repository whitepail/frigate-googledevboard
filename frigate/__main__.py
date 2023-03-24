import faulthandler

faulthandler.enable()
import sys
import threading

threading.current_thread().name = "frigate"

from frigate.app import FrigateApp
from frigate.server import ServerApp

cli = sys.modules["flask.cli"]
cli.show_server_banner = lambda *x: None

if __name__ == "__main__":
    if "server" in sys.argv:
        frigate_app = ServerApp()
    else:
        frigate_app = FrigateApp()

    frigate_app.start()
