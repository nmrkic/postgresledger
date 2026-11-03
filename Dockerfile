# pgl for CI: Python with postgresledger, pgschema, psql and the scratch Postgres 14 and 17.
# Runs as a non-root user: the scratch Postgres refuses to start as root.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home pgl

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

USER pgl
WORKDIR /home/pgl
RUN pgl setup && python -c "from postgresledger import scratch; scratch.install('14')"
