# Coarse path+method allowlist layer for the airgap Docker socket proxy
# (RFC section 9 / issue #218). Only the validator container
# (../validator/) can reach this one -- see docker-compose.yml's
# airgap-proxy-backend network, which has no default-network alias for
# this service.
#
# Pinned by digest, not tag -- same convention as postgres:18 and
# rust:1.95-bookworm elsewhere in this repo's Dockerfiles.
FROM haproxy:2.9-alpine@sha256:3e29449a6beed63262e36104adf531b4e41b359f61937303f5ea8607987b3748

COPY haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg

# The base image's default non-root "haproxy" user can't open a
# root:root-owned /var/run/docker.sock (0660 -- verified against real
# Docker Desktop socket permissions). This container's entire purpose is
# to be the one thing with real socket access, so running it as root here
# is the intended privilege boundary, not an oversight -- everything
# upstream of it (the validator, then the agent) runs unprivileged.
USER root
