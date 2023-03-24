import datetime
import logging
import multiprocessing as mp
import os
import queue
import signal
import socket
import threading
from abc import ABC, abstractmethod

import numpy as np
import tflite_runtime.interpreter as tflite
from setproctitle import setproctitle
from tflite_runtime.interpreter import load_delegate

from frigate.util import EventsPerSecond, SharedMemoryFrameManager, listen, load_labels
from frigate.network import NPSocketClient, NPSocket

logger = logging.getLogger(__name__)


class ObjectDetector(ABC):
    @abstractmethod
    def detect(self, tensor_input, threshold=0.4):
        pass


class LocalObjectDetector(ObjectDetector):
    def __init__(self, tf_device=None, model_path=None, num_threads=3, labels=None):
        self.fps = EventsPerSecond()
        if labels is None:
            self.labels = {}
        else:
            self.labels = load_labels(labels)

        device_config = {"device": "usb"}
        if not tf_device is None:
            device_config = {"device": tf_device}

        edge_tpu_delegate = None

        if tf_device != "cpu":
            try:
                logger.info(f"Attempting to load TPU as {device_config['device']}")
                edge_tpu_delegate = load_delegate("libedgetpu.so.1.0", device_config)
                logger.info("TPU found")
                self.interpreter = tflite.Interpreter(
                    model_path=model_path or "/edgetpu_model.tflite",
                    experimental_delegates=[edge_tpu_delegate],
                )
            except ValueError:
                logger.error(
                    "No EdgeTPU was detected. If you do not have a Coral device yet, you must configure CPU detectors."
                )
                raise
        else:
            logger.warning(
                "CPU detectors are not recommended and should only be used for testing or for trial purposes."
            )
            self.interpreter = tflite.Interpreter(
                model_path=model_path or "/cpu_model.tflite", num_threads=num_threads
            )

        self.interpreter.allocate_tensors()

        self.tensor_input_details = self.interpreter.get_input_details()
        self.tensor_output_details = self.interpreter.get_output_details()

    def detect(self, tensor_input, threshold=0.4):
        detections = []

        raw_detections = self.detect_raw(tensor_input)

        for d in raw_detections:
            if d[1] < threshold:
                break
            detections.append(
                (self.labels[int(d[0])], float(d[1]), (d[2], d[3], d[4], d[5]))
            )
        self.fps.update()
        return detections

    def detect_raw(self, tensor_input):
        self.interpreter.set_tensor(self.tensor_input_details[0]["index"], tensor_input)
        self.interpreter.invoke()

        boxes = self.interpreter.tensor(self.tensor_output_details[0]["index"])()[0]
        class_ids = self.interpreter.tensor(self.tensor_output_details[1]["index"])()[0]
        scores = self.interpreter.tensor(self.tensor_output_details[2]["index"])()[0]
        count = int(
            self.interpreter.tensor(self.tensor_output_details[3]["index"])()[0]
        )

        detections = np.zeros((20, 6), np.float32)

        for i in range(count):
            if scores[i] < 0.4 or i == 20:
                break
            detections[i] = [
                class_ids[i],
                float(scores[i]),
                boxes[i][0],
                boxes[i][1],
                boxes[i][2],
                boxes[i][3],
            ]

        return detections


def run_detector(
    name: str,
    detection_queue: mp.Queue,
    out_events: dict[str, mp.Event],
    avg_speed,
    start,
    model_path,
    model_shape,
    tf_device,
    num_threads,
):
    threading.current_thread().name = f"detector:{name}"
    logger = logging.getLogger(f"detector.{name}")
    logger.info(f"Starting detection process: {os.getpid()}")
    setproctitle(f"frigate.detector.{name}")
    listen()

    stop_event = mp.Event()

    def receiveSignal(signalNumber, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, receiveSignal)
    signal.signal(signal.SIGINT, receiveSignal)

    frame_manager = SharedMemoryFrameManager()
    object_detector = LocalObjectDetector(
        tf_device=tf_device, model_path=model_path, num_threads=num_threads
    )

    outputs = {}
    for name in out_events.keys():
        out_shm = mp.shared_memory.SharedMemory(name=f"out-{name}", create=False)
        out_np = np.ndarray((20, 6), dtype=np.float32, buffer=out_shm.buf)
        outputs[name] = {"shm": out_shm, "np": out_np}

    while not stop_event.is_set():
        try:
            connection_id = detection_queue.get(timeout=5)
        except queue.Empty:
            continue
        input_frame = frame_manager.get(
            connection_id, (1, model_shape[0], model_shape[1], 3)
        )

        if input_frame is None:
            continue

        # detect and send the output
        start.value = datetime.datetime.now().timestamp()
        detections = object_detector.detect_raw(input_frame)
        duration = datetime.datetime.now().timestamp() - start.value
        outputs[connection_id]["np"][:] = detections[:]
        out_events[connection_id].set()
        start.value = 0.0

        avg_speed.value = (avg_speed.value * 9 + duration) / 10


class EdgeTPUProcess:
    def __init__(
        self,
        name,
        detection_queue,
        out_events,
        model_path,
        model_shape,
        tf_device=None,
        num_threads=3,
        entrypoint=run_detector
    ):
        self.name = name
        self.out_events = out_events
        self.detection_queue = detection_queue
        self.avg_inference_speed = mp.Value("d", 0.01)
        self.detection_start = mp.Value("d", 0.0)
        self.detect_process = None
        self.model_path = model_path
        self.model_shape = model_shape
        self.tf_device = tf_device
        self.num_threads = num_threads
        self.entrypoint = entrypoint
        self.start_or_restart()

    def stop(self):
        self.detect_process.terminate()
        logging.info("Waiting for detection process to exit gracefully...")
        self.detect_process.join(timeout=30)
        if self.detect_process.exitcode is None:
            logging.info("Detection process didnt exit. Force killing...")
            self.detect_process.kill()
            self.detect_process.join()

    def start_or_restart(self):
        self.detection_start.value = 0.0
        if (not self.detect_process is None) and self.detect_process.is_alive():
            self.stop()
        self.detect_process = mp.Process(
            target=self.entrypoint,
            name=f"detector:{self.name}",
            args=(
                self.name,
                self.detection_queue,
                self.out_events,
                self.avg_inference_speed,
                self.detection_start,
                self.model_path,
                self.model_shape,
                self.tf_device,
                self.num_threads,
            ),
        )
        self.detect_process.daemon = True
        self.detect_process.start()

def run_remote(
    name: str,
    detection_queue: mp.Queue,
    out_events: Dict[str, mp.Event],
    avg_speed,
    start,
    model_path,
    model_shape,
    tf_device,
    num_threads,
):
    threading.current_thread().name = f"detector:{name}"
    logger = logging.getLogger(f"detector.{name}")
    logger.info(f"Starting detection process: {os.getpid()}")
    setproctitle(f"frigate.detector.{name}")
    listen()

    stop_event = mp.Event()

    def receiveSignal(signalNumber, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, receiveSignal)
    signal.signal(signal.SIGINT, receiveSignal)

    frame_manager = SharedMemoryFrameManager()

    outputs = {}
    for name in out_events.keys():
        out_shm = mp.shared_memory.SharedMemory(name=f"out-{name}", create=False)
        out_np = np.ndarray((20, 6), dtype=np.float32, buffer=out_shm.buf)
        outputs[name] = {"shm": out_shm, "np": out_np}

    while not stop_event.is_set():
        #try:
        #    while not stop_event.is_set():
        con = NPSocketClient(tf_device.split(':'))

        while not stop_event.is_set():
            try:
                connection_id = detection_queue.get(timeout=5)
            except queue.Empty:
                continue

            input_frame = frame_manager.get(
                connection_id, (1, model_shape[0], model_shape[1], 3)
            )

            if input_frame is None:
                continue

            # detect and send the output
            start.value = datetime.datetime.now().timestamp()

            con.send(a=input_frame)
            detections = con.recv()

            duration = datetime.datetime.now().timestamp() - start.value
            outputs[connection_id]["np"][:] = detections[:]
            out_events[connection_id].set()
            start.value = 0.0

            avg_speed.value = (avg_speed.value * 9 + duration) / 10

class EdgeTPUConnection(EdgeTPUProcess):
    def __init__(
        self,
        name,
        detection_queue,
        out_events,
        model_path,
        model_shape,
        tf_device=None,
        num_threads=3,
    ):
        super().__init__(name, detection_queue, out_events, model_path, model_shape, tf_device, num_threads, run_remote)

class RemoteObjectDetector:
    def __init__(self, name, labels, detection_queue, event, model_shape):
        self.labels = labels
        self.name = name
        self.fps = EventsPerSecond()
        self.detection_queue = detection_queue
        self.event = event
        self.shm = mp.shared_memory.SharedMemory(name=self.name, create=False)
        self.np_shm = np.ndarray(
            (1, model_shape[0], model_shape[1], 3), dtype=np.uint8, buffer=self.shm.buf
        )
        self.out_shm = mp.shared_memory.SharedMemory(
            name=f"out-{self.name}", create=False
        )
        self.out_np_shm = np.ndarray((20, 6), dtype=np.float32, buffer=self.out_shm.buf)

    def detect(self, tensor_input, threshold=0.4):
        detections = []

        # copy input to shared memory
        self.np_shm[:] = tensor_input[:]
        self.event.clear()
        self.detection_queue.put(self.name)
        result = self.event.wait(timeout=10.0)

        # if it timed out
        if result is None:
            return detections

        for d in self.out_np_shm:
            if d[1] < threshold:
                break
            detections.append(
                (self.labels[int(d[0])], float(d[1]), (d[2], d[3], d[4], d[5]))
            )
        self.fps.update()
        return detections

    def cleanup(self):
        self.shm.unlink()
        self.out_shm.unlink()
