FROM coturn/coturn:latest

# envsubst is in gettext-base (Debian-based image)
USER root
RUN apt-get update && apt-get install -y --no-install-recommends gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Copy config template and entrypoint
COPY turnserver.conf /etc/coturn/turnserver.conf.tmpl
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# STUN/TURN (plain)
EXPOSE 3478/udp
EXPOSE 3478/tcp

# TURN alternate plain ports
EXPOSE 3479/udp
EXPOSE 3479/tcp

# STUN/TURN over TLS
EXPOSE 5349/tcp
EXPOSE 5349/udp
EXPOSE 5350/tcp
EXPOSE 5350/udp

# Media relay range
EXPOSE 49152-65535/udp

ENTRYPOINT ["/entrypoint.sh"]
