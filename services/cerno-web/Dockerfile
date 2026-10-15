FROM node:24-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev 2>/dev/null || npm install --omit=dev

FROM node:24-slim AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci 2>/dev/null || npm install
COPY . .
RUN npm run build

FROM node:24-slim AS run
WORKDIR /app
# standalone 서버는 next start -p 를 타지 않는다 — PORT env 가 유일한 포트 지정 수단
ENV NODE_ENV=production PORT=3400 HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER node
EXPOSE 3400
CMD ["node", "server.js"]
