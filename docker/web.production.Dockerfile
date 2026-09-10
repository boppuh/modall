FROM node:22.20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci

COPY apps/web ./apps/web

ENV VITE_API_BASE_URL=/
RUN npm run web:build

FROM nginx:1.29.1-alpine

COPY deploy/cloudflare/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/apps/web/dist /usr/share/nginx/html

EXPOSE 8080
