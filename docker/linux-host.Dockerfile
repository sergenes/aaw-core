# A clean Linux box for testing the host by hand: Ubuntu, tmux, Python, aaw-core from this
# checkout. See docker/README.md for the walkthrough (relay + host containers, then a
# phone pointed at the relay).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tmux python3 python3-venv python3-pip ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# An unprivileged user, as on a real machine (aaw refuses to misbehave as root).
RUN useradd -m -s /bin/bash dev
USER dev
WORKDIR /home/dev

COPY --chown=dev:dev . /home/dev/aaw-core
RUN python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -e "aaw-core[dev,relay]"
ENV PATH="/home/dev/.venv/bin:/home/dev/.local/bin:${PATH}"

CMD ["bash"]
