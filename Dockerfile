# Same recipe as fiji.def, for Docker. `restore` installs the locked JDK into
# /opt/Fiji/java/linux64/. If your lockfile has no "java.packages", use a JDK base
# image (e.g. azul/zulu-openjdk:21) and add --no-java to the restore line.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates fontconfig libfreetype6 libxrender1 libxtst6 libxi6 \
 && rm -rf /var/lib/apt/lists/*

# fiji.local.* matches the optional bundle without failing if it is absent
COPY fiji_lock.py fiji.lock.json fiji.local.* /opt/fiji-lock/

RUN BUNDLE=""; [ -f /opt/fiji-lock/fiji.local.zip ] && BUNDLE="--local-bundle /opt/fiji-lock/fiji.local.zip"; \
    python3 /opt/fiji-lock/fiji_lock.py restore /opt/fiji-lock/fiji.lock.json /opt/Fiji --platform linux64 $BUNDLE \
 && python3 /opt/fiji-lock/fiji_lock.py verify /opt/fiji-lock/fiji.lock.json /opt/Fiji --platform linux64

ENTRYPOINT ["/bin/sh", "-c", "L=/opt/Fiji/fiji-linux-x64; [ -x $L ] || L=/opt/Fiji/ImageJ-linux64; exec $L --headless \"$@\"", "--"]
