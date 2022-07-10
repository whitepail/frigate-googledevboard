import logging
from queue import Queue
import socket
import sys
import threading
import traceback
import pydantic
import os

import numpy as np

from frigate.config import DetectorConfig
from frigate.edgetpu import LocalObjectDetector
from frigate.app import FrigateApp, VERSION
from frigate.config import DetectorTypeEnum, ServerConfig
from frigate.network import NPSocketServer, NPSocket

logger = logging.getLogger(__name__)

class ServerApp(FrigateApp):
    def init_config(self):
        config_file = os.environ.get("CONFIG_FILE", "/config/config.yml")

        # Check if we can use .yaml instead of .yml
        config_file_yaml = config_file.replace(".yml", ".yaml")
        if os.path.isfile(config_file_yaml):
            config_file = config_file_yaml

        user_config = ServerConfig.parse_file(config_file)
        self.config = user_config.runtime_config

    def start(self):
        logger.info(f"Starting Frigate ({VERSION})")
        try:
            try:
                self.init_config()
            except Exception as e:
                print("*************************************************************")
                print("*************************************************************")
                print("***    Your config file is not valid!                     ***")
                print("***    Please check the docs at                           ***")
                print("***    https://docs.frigate.video/configuration/index     ***")
                print("*************************************************************")
                print("*************************************************************")
                print("***    Config Validation Errors                           ***")
                print("*************************************************************")
                print(e.__class__.__name__)
                print(e)
                print(traceback.format_exc())
                print("*************************************************************")
                print("***    End Config Validation Errors                       ***")
                print("*************************************************************")
                sys.exit(1)
            self.set_log_levels()
        except Exception as e:
            print(e)
            print(traceback.format_exc())
            sys.exit(1)

        detector : DetectorConfig = self.config.serve.detector

        if detector.type == DetectorTypeEnum.cpu:
            self.LOD = LocalObjectDetector(tf_device="cpu", model_path=self.config.model.path, num_threads=detector.num_threads)
        elif detector.type == DetectorTypeEnum.edgetpu:
            self.LOD = LocalObjectDetector(tf_device=detector.device, model_path=self.config.model.path, num_threads=detector.num_threads)
        else:
            raise Exception("Not implemented")

        print(f"Listening on {self.config.serve.port}")
        srv = NPSocketServer(('', self.config.serve.port), self.handle_conn)

        self.lock = threading.Lock()
        self.N = 0

        srv.listen_and_serve()

    def handle_conn(self, con:NPSocket):
        print(f"Connected {con.con.getpeername()}")
        while True:
            a = con.recv()
            with self.lock:
                self.N += 1
                if self.N % 100 == 0:
                    logger.info(f"Handled {self.N} requests")
                a = self.LOD.detect_raw(a)
            con.send(a)
