ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# The release pipeline must pass a digest-pinned BASE_IMAGE.  The local tag is
# intentionally convenient for development; SandboxPolicy enforces the same
# requirement in every deployment.
RUN useradd --uid 65532 --create-home --shell /usr/sbin/nologin pleiad
WORKDIR /workspace
USER 65532:65532
