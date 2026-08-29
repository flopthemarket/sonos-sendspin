ARG BUILD_FROM
FROM ${BUILD_FROM:-ghcr.io/home-assistant/amd64-base:3.19}

# ffmpeg provides the encoders (flac/mp3/wav) used by the audio pipeline
RUN apk add --no-cache \
        python3 \
        py3-pip \
        ffmpeg \
        curl

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app/ /app/app/
COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh

EXPOSE 8099

ENTRYPOINT ["/app/run.sh"]
