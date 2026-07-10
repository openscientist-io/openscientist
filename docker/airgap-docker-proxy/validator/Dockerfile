# Body-inspection layer for the airgap Docker socket proxy (RFC section 9 /
# issue #218). The only new security-critical code in this PR -- see
# validator.py's module docstring. Runs unprivileged (no docker.sock
# access at all; only reaches the haproxy service on the internal
# airgap-proxy-backend network -- see docker-compose.yml).
FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

RUN pip install --no-cache-dir aiohttp==3.11.11

COPY validator.py /app/validator.py
WORKDIR /app

USER nobody
CMD ["python", "validator.py"]
