FROM python:3.12-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake libgmp-dev libboost-python-dev libboost-system-dev \
    libssl-dev git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY chia /app/chia
COPY pyproject.toml README.md LICENSE /app/
ENV POETRY_DYNAMIC_VERSIONING_BYPASS=0.0.0
RUN pip install --no-cache-dir .
# Build the executable explicitly: recent chiavdf wheels/source installs only
# install the Python verifier, while Chia's launcher needs a sibling vdf_client.
RUN mkdir /tmp/vdf && \
    pip download --no-cache-dir --no-deps --no-binary chiavdf -d /tmp/vdf \
      "chiavdf==$(python -c 'from importlib.metadata import version; print(version("chiavdf"))')" && \
    tar -xzf /tmp/vdf/chiavdf-*.tar.gz -C /tmp/vdf && \
    make -C /tmp/vdf/chiavdf-*/src -f Makefile.vdf-client vdf_client && \
    cp /tmp/vdf/chiavdf-*/src/vdf_client \
      "$(python -c 'import pathlib, chiavdf; print(pathlib.Path(chiavdf.__file__).parent)')/vdf_client" && \
    rm -rf /tmp/vdf && \
    python -c 'from chia.timelord.timelord_launcher import find_vdf_client; print(find_vdf_client())'
COPY docker/private-network /app/docker/private-network
ENV CHIA_ROOT=/data/chia CHIA_KEYS_ROOT=/data/keys PYTHONUNBUFFERED=1
ENTRYPOINT ["bash", "/app/docker/private-network/entrypoint.sh"]
