# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
#
# BUILD_FROM is supplied automatically by the Supervisor build system per
# architecture, using the mapping in build.yaml - no manual TARGETARCH
# branching needed here.
ARG BUILD_FROM
FROM ${BUILD_FROM}

# ffmpeg provides the encoders (flac/mp3/wav) used by the audio pipeline.
# python3/py3-pip run the gateway itself.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        ffmpeg \
        curl

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app/ /app/app/

# s6-overlay service definition (the HA base image's entrypoint is s6's
# /init, which runs everything under /etc/services.d/ - we don't set our
# own ENTRYPOINT/CMD so that default is preserved).
COPY rootfs /

RUN chmod a+x /etc/services.d/spin_sonos_gateway/run

EXPOSE 8099

LABEL \
    org.opencontainers.image.title="Home Assistant Add-on: Sendspin Sonos Gateway" \
    org.opencontainers.image.description="Bridges a Sendspin audio stream to Sonos speakers with a live, glitch-free delay control." \
    org.opencontainers.image.source="https://github.com/flopthemarket/sonos-sendspin" \
    org.opencontainers.image.licenses="Apache-2.0"
