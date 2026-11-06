# syntax=docker/dockerfile:1.7
# NuroFlow dashboard: static SPA served by unprivileged nginx, which also proxies /api
# to the backend. One origin for UI and API means no CORS and a strict CSP.

# ---------------------------------------------------------------- 1. build
FROM node:22.11-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---------------------------------------------------------------- 2. serve
# nginx-unprivileged: runs as uid 101 and listens on 8080 (no root, no CAP_NET_BIND).
FROM nginxinc/nginx-unprivileged:1.30-alpine AS runtime
LABEL org.opencontainers.image.title="nuroflow-web" \
      org.opencontainers.image.source="https://github.com/Nuroart/nuroflow" \
      org.opencontainers.image.licenses="MIT"
# Patch OS packages published after the base image was built (CI rebuilds weekly so
# fixes land without waiting for upstream), and remove curl: nothing here uses it
# (the healthcheck uses BusyBox wget), so it is pure attack surface.
USER root
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    apk upgrade --no-cache \
 && (apk del --no-cache curl libcurl 2>/dev/null || true)
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/nginx-health || exit 1
