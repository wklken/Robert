ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE} AS wheel
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --wheel-dir /wheels . \
    && sha256sum /wheels/robert_github_agent-*.whl > /wheels/wheel.sha256

FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git procps ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home harness \
    && install -d -o harness -g harness /state /git /fixtures
COPY --from=wheel /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels robert-github-agent \
    && mv /wheels/wheel.sha256 /usr/local/share/robert-wheel.sha256 \
    && rm -rf /wheels
COPY tests/simulations/compose/*.py /harness/
RUN ln -s /harness/fake_gh.py /usr/local/bin/gh && chmod +x /harness/fake_gh.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/harness
USER harness
WORKDIR /harness
