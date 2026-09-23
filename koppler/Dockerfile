FROM node:22-bookworm-slim

# Chromium aus der Distribution statt des von Puppeteer geladenen: das Paket
# gibt es fuer amd64 UND arm64, und es wird mit dem System aktualisiert.
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    CHROMIUM=/usr/bin/chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium ca-certificates fonts-liberation patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# Korrektur an whatsapp-web.js, die es noch nicht veroeffentlicht gibt.
# Begruendung und Herkunft stehen im Kopf der Patch-Datei.
#
# Kein --forward: npm install laeuft hier in ein leeres Abbild, der Patch kann
# also nie schon drin sein. Passt er nicht mehr -- weil die Version angehoben
# wurde oder der PR zusammengefuehrt ist --, soll der Bau abbrechen. Ein still
# uebersprungener Patch ergaebe ein Abbild, das aussieht wie repariert und es
# nicht ist.
#
# Die beiden Pruefungen danach sind kein Zierrat: patch meldet auch dann
# Erfolg, wenn es Brocken mit Versatz unterbringt. node --check zeigt, dass
# die Datei noch gueltig ist, und grep, dass die neue Hilfsfunktion wirklich
# drinsteht.
COPY patches ./patches
RUN patch -p1 -d node_modules/whatsapp-web.js \
      < patches/whatsapp-web.js-1.34.7-serialized.patch \
    && node --check node_modules/whatsapp-web.js/src/util/Injected/Utils.js \
    && grep -q 'getMsgKeyId' node_modules/whatsapp-web.js/src/util/Injected/Utils.js

COPY server.js ./

# Die Sitzung liegt ausserhalb des Abbilds. Geht sie verloren, muss der
# QR-Code neu gescannt werden -- und ein verlorener Kopplungszustand ist
# genau das, was man beim Neubau eines Containers nicht erleben will.
VOLUME ["/daten"]
ENV KOPPLER_DATEN=/daten/koppler

EXPOSE 8098
CMD ["node", "server.js"]
