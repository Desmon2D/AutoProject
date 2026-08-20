FROM node:24-bookworm-slim AS harness-runtime

ARG DSH_VERSION=0.1.0-rc.7

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        tini \
    && npm install --global \
        --allow-scripts="@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs" \
        "@deepseek-ai/dsh@${DSH_VERSION}" \
    && npm cache clean --force \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid sandbox --create-home --shell /bin/sh sandbox \
    && mkdir -p /job/input /workspace /output /opt/sandbox \
    && chown -R sandbox:sandbox /job /workspace /output /opt/sandbox \
    && rm -rf /var/lib/apt/lists/*

COPY sandbox/plugins/step-result /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@automation/dsh-plugin-step-result
RUN node --test /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@automation/dsh-plugin-step-result/test/index.test.js

ENV DSH_HOME=/home/sandbox/.dsh \
    NODE_ENV=production

WORKDIR /workspace
USER sandbox

COPY --chown=sandbox:sandbox sandbox/runner.py /opt/sandbox/runner.py
COPY --chown=sandbox:sandbox sandbox/contracts /opt/sandbox/contracts
COPY --chown=sandbox:sandbox --chmod=755 sandbox/dsh-wrapper.sh /opt/sandbox/dsh-wrapper.sh
COPY --chown=sandbox:sandbox --chmod=755 sandbox/git-askpass.py /opt/sandbox/git-askpass.py
COPY --chown=sandbox:sandbox sandbox/image-profiles/core.json /opt/sandbox/image-manifest.json
COPY --chown=sandbox:sandbox sandbox/tests /opt/sandbox/tests
RUN python3 -m unittest discover -s /opt/sandbox/tests

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/sandbox/runner.py"]

FROM harness-runtime AS core

FROM harness-runtime AS code

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=sandbox:sandbox sandbox/image-profiles/code.json /opt/sandbox/image-manifest.json
USER sandbox

FROM code AS delivery

USER root
COPY orchestrator/plugins/gitea/source /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@automation/dsh-plugin-gitea
COPY orchestrator/plugins/swirl/source /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@automation/dsh-plugin-swirl
RUN node --test /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@automation/dsh-plugin-gitea/test/index.test.js \
    && node --test /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@automation/dsh-plugin-swirl/test/index.test.js
COPY --chown=sandbox:sandbox sandbox/image-profiles/delivery.json /opt/sandbox/image-manifest.json
USER sandbox
