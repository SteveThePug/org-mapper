# The public entry point: Caddy serves the built React app, proxies /api to the backend, and
# handles HTTPS in production. Compose passes the frontend source in as a second build context
# named "frontend" (see `additional_contexts` in docker-compose.yml), so this image can build
# the site without the proxy config living inside frontend/.

# Stage 1: build the static site. Only dist/ is kept; Node and node_modules are thrown away.
FROM node:24-slim AS site
WORKDIR /app
COPY --from=frontend package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --from=frontend . .
RUN npm run build

# Stage 2: Caddy plus the built files.
FROM caddy:2.10-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=site /app/dist /srv
