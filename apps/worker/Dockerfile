# apps/worker/Dockerfile
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install poetry==1.8.3 --no-cache-dir

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi

COPY app/ ./app/

FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    nodejs \
    npm \
    gosu \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# --- gltf-pipeline: Draco compression for GLTF/STEP pipelines ---
RUN npm install -g gltf-pipeline

# --- xeokit-convert: IFC -> XKT conversion --------------------------------
# @xeokit/xeokit-convert does NOT publish a global CLI binary named
# "xeokit-convert" — its entry point is convert2xkt.js, invoked via
# `node <path>/convert2xkt.js`. Worker code (app/config.py::XEOKIT_CONVERT_BIN,
# default "xeokit-convert") expects a bare command on PATH, so install the
# package and generate a thin shim rather than changing application code.
#
# Version pinned (previously unpinned, unlike gltf-validator below) —
# an unpinned `npm install -g` here means a future `latest` publish could
# silently change what gets installed between builds, with no way to tell
# from the Dockerfile alone. Confirm this version against the actual
# registry before relying on it; it's a placeholder pin, not a verified one.
ARG XEOKIT_CONVERT_VERSION=1.0.6
RUN npm install -g "@xeokit/xeokit-convert@${XEOKIT_CONVERT_VERSION}" \
    && CONVERT2XKT="$(npm root -g)/@xeokit/xeokit-convert/convert2xkt.js" \
    && test -f "$CONVERT2XKT" \
    # Build-time self-test: if convert2xkt.js is actually a broken/stub
    # file (the exact failure mode this comment block exists to explain),
    # `node convert2xkt.js` fails on argument parsing or a syntax error
    # immediately here — the build stops with a clear message instead of
    # shipping a worker image that fails silently on the first real upload.
    && (node "$CONVERT2XKT" --help > /tmp/xeokit_selftest.log 2>&1 || true) \
    && grep -qi "usage\|xkt\|options" /tmp/xeokit_selftest.log || \
       (echo "FATAL: xeokit-convert self-test failed — convert2xkt.js did not" \
             "produce expected --help output. Contents of $CONVERT2XKT:" \
        && cat "$CONVERT2XKT" \
        && exit 1) \
    && rm -f /tmp/xeokit_selftest.log

# --- gltf-validator: native Dart-compiled CLI, NOT the npm library -------
# The npm package "gltf-validator" is a JS *library* (validator.validateBytes),
# not a CLI — it does not expose `gltf-validator --stdout <file>`, which is
# exactly the invocation app/tasks/gltf.py uses. The real CLI is a separate
# native binary distributed on GitHub Releases. Pinned version for
# reproducible builds.
ARG GLTF_VALIDATOR_VERSION=2.0.0-dev.3.10
RUN curl -fsSL \
      "https://github.com/KhronosGroup/glTF-Validator/releases/download/${GLTF_VALIDATOR_VERSION}/gltf_validator-${GLTF_VALIDATOR_VERSION}-linux64.tar.xz" \
      -o /tmp/gltf_validator.tar.xz \
    && tar -xf /tmp/gltf_validator.tar.xz -C /usr/local/bin gltf_validator \
    && ln -s /usr/local/bin/gltf_validator /usr/local/bin/gltf-validator \
    && chmod +x /usr/local/bin/gltf_validator \
    && rm /tmp/gltf_validator.tar.xz

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app

RUN groupadd -r appuser \
    && useradd -r -g appuser -d /app -s /usr/sbin/nologin appuser \
    && mkdir -p /data/openformat \
    && chown -R appuser:appuser /app /data/openformat

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Intentionally NOT `USER appuser` here — the container must start as root
# so entrypoint.sh can fix ownership of PROMETHEUS_MULTIPROC_DIR (a tmpfs
# mounted fresh, root-owned, at container start — see entrypoint.sh for
# why this can't be done at image build time). entrypoint.sh drops to
# appuser via gosu before the real process starts.
ENTRYPOINT ["/entrypoint.sh"]

CMD ["celery", "-A", "app.celery_app.celery_app", "worker", \
     "--loglevel=info", \
     "--queues=ifc,mesh,scan,bcf,webhook", \
     "--concurrency=2"]