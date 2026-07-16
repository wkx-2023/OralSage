FROM docker.1ms.run/library/node:22-alpine

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /srv/clawbridge
COPY clawbridge/package.json ./
RUN npm config set registry ${NPM_REGISTRY} \
    && npm install --omit=dev
COPY clawbridge/ ./
RUN mkdir -p /var/lib/clawbridge

ENV NODE_ENV=production
EXPOSE 8787

CMD ["npm", "run", "start"]
