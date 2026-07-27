FROM node:22.14-alpine

WORKDIR /app

COPY package.json .

RUN npm install

RUN npm i -g serve

COPY . .

ARG VITE_ORCHESTRATOR_API_ENDPOINT
ENV VITE_ORCHESTRATOR_API_ENDPOINT=${VITE_ORCHESTRATOR_API_ENDPOINT}

RUN npm run build  

EXPOSE 80

CMD [ "serve", "-s", "dist", "-l", "80" ]