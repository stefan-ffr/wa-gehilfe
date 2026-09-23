# --- Stilblatt bauen ---------------------------------------------------------
#
# Tailwind laeuft HIER, nicht im Browser. Das CDN waere ein Skript von einem
# fremden Server auf jeder Seite -- genau die Abhaengigkeit, die sonst
# ueberall vermieden wird. So liegt am Ende eine fertige CSS-Datei im Abbild,
# und der Dienst laeuft auch ohne Internet.
#
# Die Klassennamen stehen in den f-Strings von main.py; die Datei wird
# deshalb mitkopiert und in eingang.css als @source genannt.
FROM node:22-slim AS stil
WORKDIR /bau
COPY app/stil/ ./app/stil/
COPY app/main.py ./app/main.py
RUN npm install --no-audit --no-fund --no-save @tailwindcss/cli@^4 \
 && npx @tailwindcss/cli -i app/stil/eingang.css -o app/static/app.css --minify \
 && test -s app/static/app.css \
 && grep -q 'rounded' app/static/app.css

# --- Dienst ------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --from=stil /bau/app/static/ ./app/static/

# Der Speicher liegt ausserhalb des Abbilds: das Gedaechtnis soll einen
# Neubau des Containers ueberleben.
VOLUME ["/daten"]
ENV ENTWURF_DB=/daten/entwuerfe.sqlite3

EXPOSE 8099
# Standardmaessig NUR auf der Schleife. Teilt sich der Container das Hostnetz
# mit einem Tunnel-Beiwagen, haette 0.0.0.0 bedeutet, dass jeder im selben Netz
# den Dienst direkt erreicht und an einem vorgeschalteten Zugangsschutz
# vorbeikommt. Fuer docker-compose (eigenes Netz je Container) setzt die
# Compose-Datei BIND_ADRESSE=0.0.0.0.
ENV BIND_ADRESSE=127.0.0.1
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${BIND_ADRESSE} --port 8099"]
