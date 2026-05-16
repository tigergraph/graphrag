FROM node:23.7-slim
WORKDIR /app

ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable

COPY . .

# Pull the shared VERSION file from the repo root (exposed via the
# ``repo`` additional build context). Compose ``docker compose build``
# resolves it automatically; no env vars or wrapper script needed.
COPY --from=repo VERSION ./public/version
RUN mkdir -p public && \
    echo "{\"component\":\"graphrag-ui\",\"version\":\"$(cat public/version | tr -d '\n')\",\"build_date\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > public/version.json && \
    rm public/version

RUN pnpm install
RUN pnpm run build
RUN pnpm i -g serve


CMD [ "serve", "-s", "dist" ]
