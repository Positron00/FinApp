# syntax=docker/dockerfile:1
# The web image (platform LLD §10.2; C4). Build from the repo root:
#   docker build -f apps/web/Dockerfile -t payrun/web:dev .
# No secrets at build time: config is parsed lazily at server start (lib/config.ts).
ARG NODE_IMAGE=node:24.22.0-slim

FROM ${NODE_IMAGE} AS deps
ENV PNPM_HOME=/pnpm COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=1
RUN corepack enable
WORKDIR /repo
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/api-client/package.json packages/api-client/
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store && \
    pnpm install --frozen-lockfile --filter "web..."

FROM deps AS build
COPY apps/web apps/web
COPY packages packages
ENV NEXT_TELEMETRY_DISABLED=1
# next.config: output "standalone", outputFileTracingRoot = repo root
RUN pnpm --filter web build

FROM ${NODE_IMAGE}
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="payrun-web" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/Positron00/FinApp"
RUN useradd --system --uid 10001 --gid 0 --no-create-home app
WORKDIR /app
COPY --from=build --chown=10001:0 /repo/apps/web/.next/standalone ./
COPY --from=build --chown=10001:0 /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=10001:0 /repo/apps/web/public ./apps/web/public
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000 NEXT_TELEMETRY_DISABLED=1 APP_VERSION=${VERSION}
USER 10001
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/api/livez').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]
CMD ["node", "apps/web/server.js"]
