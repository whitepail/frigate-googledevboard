from io import BytesIO
import logging
import socket
from struct import pack, unpack
from typing import Callable, Tuple
import numpy as np
import threading

logger = logging.getLogger(__name__)

class Closed(BaseException):
    pass

class NPSocket(object):
    def __init__(self, con:socket.socket):
        self.con = con
        self.con.setblocking(True)

    def send(self, a:np.ndarray):
        buf = BytesIO()
        np.save(buf, a, allow_pickle=False)

        data = buf.getvalue()
        self.con.sendall(pack("!i", len(data)))
        self.con.sendall(data)

    def recv(self) -> np.ndarray:
        buf = self.con.recv(4, socket.MSG_WAITALL)
        if len(buf)!=4:
            self.con.close()
            raise Closed

        length = unpack("!i", buf)[0]
        data = b''
        remain = length

        while remain > 0:
            n = 1024
            if n>remain:
                n = remain

            data += self.con.recv(n)

            remain = length - len(data)

        return np.load(BytesIO(data))

class NPSocketClient(NPSocket):
    def __init__(self, addr:Tuple[str,int]):
        super().__init__(socket.create_connection(addr))
        logger.info("New Client")

class NPSocketServer(object):
    def __init__(self, addr:Tuple[str,int], handler:Callable[["NPSocket"], None]) -> None:
        self.con = socket.create_server(addr)
        self.handler = handler
        logger.info("New Server")

    def listen_and_serve(self):
        def handler(sock:NPSocket, H:Callable[[NPSocket], None]):
            try:
                H(sock)
            except Closed:
                pass
            except BaseException as b:
                logger.warn("Handler failed")
                raise
            logger.info("Closed")
            sock.con.close()

        while True:
            client, _ = self.con.accept()

            sock = NPSocket(client)
            #handler(sock, self.handler)

            t=threading.Thread(target=handler, args=[sock, self.handler], daemon=True)
            t.start()

if __name__ == "__main__":
    srv = NPSocketServer(('', 9999), print)