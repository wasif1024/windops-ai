# The browser's application: built with Node, then served by an unprivileged nginx (DEP-001).
#   docker build -t windops-frontend:local frontend
FROM node:24.19.0-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS build
WORKDIR /app
COPY package.json package-lock.json ./
# Exactly the locked versions, with no install scripts run.
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build:app

FROM nginxinc/nginx-unprivileged:1.31.7-alpine@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
# Run by the image's entrypoint before nginx starts: it writes /config.json from the settings.
COPY --chmod=755 nginx/40-windops-config.sh /docker-entrypoint.d/40-windops-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
