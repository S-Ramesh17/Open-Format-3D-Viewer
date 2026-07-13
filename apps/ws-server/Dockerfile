# apps/ws-server/Dockerfile
FROM node:20-slim AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:20-slim AS runtime

WORKDIR /app

COPY --from=builder /app/node_modules ./node_modules
COPY package.json ./
COPY src/ ./src/

RUN chown -R node:node /app

EXPOSE 8001
USER node

CMD ["node", "src/server.js"]