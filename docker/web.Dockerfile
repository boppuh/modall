FROM node:22.20-alpine

WORKDIR /app

COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci

COPY apps/web ./apps/web

RUN chown -R node:node /app
USER node

CMD ["npm", "run", "web:dev"]
