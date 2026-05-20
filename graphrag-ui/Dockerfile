FROM node:23.7-slim
WORKDIR /app

# Use npm install (not npm ci) to handle cross-platform lockfile differences (Windows host → Linux container).
COPY package.json package-lock.json ./
RUN npm install --legacy-peer-deps

COPY . .
RUN npm run build
RUN npm i -g serve

CMD [ "serve", "-s", "dist" ]
