# =============================================
# AutoCVE Frontend Docker 构建
# =============================================
# 使用 Nginx 提供静态文件和反向代理 (支持 SSE 流式传输)

FROM node:20-slim AS builder

WORKDIR /app

ARG VITE_ENABLE_CHECKMARX_SCAN=false
ARG VITE_APP_VERSION

# 彻底清除代理设置
ENV http_proxy=""
ENV https_proxy=""
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""
ENV all_proxy=""
ENV ALL_PROXY=""
ENV no_proxy="*"
ENV NO_PROXY="*"

RUN npm config set registry https://registry.npmmirror.com && \
    npm install -g pnpm@9.15.9 && \
    pnpm config set registry https://registry.npmmirror.com

COPY package.json pnpm-lock.yaml ./

RUN pnpm config set fetch-timeout 300000 && \
    pnpm config set fetch-retries 5 && \
    pnpm install --no-frozen-lockfile --network-concurrency 1

COPY . .

ENV VITE_API_BASE_URL=__API_BASE_URL__
ENV VITE_ENABLE_CHECKMARX_SCAN=${VITE_ENABLE_CHECKMARX_SCAN}
ENV VITE_APP_VERSION=${VITE_APP_VERSION}

# 构建生产版本
RUN pnpm build

# =============================================
# 生产镜像 - 使用 Nginx (支持 SSE 反向代理)
# =============================================
FROM nginx:alpine

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 复制 Nginx 配置 (包含 SSE 反向代理配置)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 复制启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
