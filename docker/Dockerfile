ARG FRIGATE_BASE_IMAGE=whitepail/mendel-5.3:latest
FROM blakeblackshear/frigate-nginx:1.0.2 as nginx

FROM node:16 as web

WORKDIR /opt/frigate

COPY web/ .

RUN npm install && npm run build

FROM whitepail/mendel-5.3:latest as wheels

ENV DEBIAN_FRONTEND=noninteractive

RUN pip3 install scikit-build

# TODO: lock with requirements.txt
RUN pip3 wheel --wheel-dir=/wheels \
    opencv-python-headless==4.5.5.64 \
    numpy==1.23.5 \
    imutils \
    scipy \
    psutil \
    Flask \
    paho-mqtt \
    PyYAML \
    matplotlib \
    click \
    setproctitle \
    peewee \
    peewee_migrate \
    pydantic \
    zeroconf \
    ws4py \
    tflite-runtime==2.9.1

# Frigate Container
FROM ${FRIGATE_BASE_IMAGE}
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV FLASK_ENV=development

RUN apt-get -qq update && apt-get -qq install --no-install-recommends -y ffmpeg

RUN apt-get install -y \
    gstreamer1.0-plugins-base-apps \
    gstreamer1.0-tools \
    gstreamer1.0-alsa \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev

COPY --from=wheels /wheels /wheels

RUN pip3 install -U /wheels/*.whl \
    && rm -rf /var/lib/apt/lists/* /wheels \
    && (apt-get autoremove -y; apt-get autoclean -y)

COPY --from=nginx /usr/local/nginx/ /usr/local/nginx/

# get model and labels
COPY labelmap.txt /labelmap.txt
RUN wget -q https://github.com/google-coral/test_data/raw/release-frogfish/ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite -O /edgetpu_model.tflite
RUN wget -q https://github.com/google-coral/test_data/raw/release-frogfish/ssdlite_mobiledet_coco_qat_postprocess.tflite -O /cpu_model.tflite

WORKDIR /opt/frigate/
ADD frigate frigate/
ADD migrations migrations/

COPY --from=web /opt/frigate/dist web/

COPY docker/rootfs/ /

# s6-overlay
RUN S6_ARCH="${TARGETARCH}" \
    && if [ "${TARGETARCH}" = "amd64" ]; then S6_ARCH="amd64"; fi \
    && if [ "${TARGETARCH}" = "arm64" ]; then S6_ARCH="aarch64"; fi \
    && wget -O /tmp/s6-overlay-installer "https://github.com/just-containers/s6-overlay/releases/download/v2.2.0.3/s6-overlay-${S6_ARCH}-installer" \
    && chmod +x /tmp/s6-overlay-installer && /tmp/s6-overlay-installer /

EXPOSE 5000
EXPOSE 1935

ENTRYPOINT ["/init"]

CMD ["python3", "-u", "-m", "frigate"]