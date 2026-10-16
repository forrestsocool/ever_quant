# EverQuant 服务器 Docker Compose & Nginx 部署手册

本手册专门针对在服务器 **`tjsj.sensen.li`** 的 `/opt/` 路径下使用 Docker Compose 部署，并通过 Nginx 反向代理配置 `*.sensen.li` 域名的完整流程。

---

## 架构概览

```
[ 用户浏览器 / AI 客户端 ]
            │ (HTTPS 443)
            ▼
    [ Nginx 反向代理 ] (radar.sensen.li)
            │ (HTTP 127.0.0.1:8765)
            ▼
[ Docker 容器: ever_quant_radar ]
    ├── 每日 09:00 / 18:30 自动拉取更新
    ├── 持久化卷挂载: /opt/ever_quant/data
    └── 环境变量读取: TUSHARE_TOKEN
```

---

## 第一步：GitHub Actions 自动构建镜像

代码已配置好 GitHub Actions 自动化工作流（`.github/workflows/docker-publish.yml`）：
- 每当代码推送到 `main` 分支时，GitHub 将自动拉起 Ubuntu 云端构建机完成 Docker 镜像打包；
- 镜像自动推送到 GitHub Container Registry (GHCR)：
  **`ghcr.io/forrestsocool/ever_quant:latest`**

> **注**：首次从 GitHub 镜像仓库拉取镜像，如仓库为私有，服务器需执行一次登录：
> ```bash
> echo "YOUR_GITHUB_PAT_TOKEN" | docker login ghcr.io -u forrestsocool --password-stdin
> ```
> 若仓库为 Public 公开，则无需登录直接拉取。

---

## 第二步：在服务器 `/opt/` 下部署 Docker Compose

登录您的服务器 `tjsj.sensen.li`：

### 1. 创建项目目录与数据持久化目录
```bash
mkdir -p /opt/ever_quant/data
cd /opt/ever_quant
```

### 2. 配置 `docker-compose.yml`
在该目录下创建 `docker-compose.yml`（或直接从项目拉取）：
```yaml
services:
  radar:
    image: ghcr.io/forrestsocool/ever_quant:latest
    container_name: ever_quant_radar
    restart: always
    ports:
      # 仅暴露给本机 Nginx 代理，避免公网端口泄露
      - "127.0.0.1:8765:8765"
    environment:
      - TZ=Asia/Shanghai
      # Tushare 认证 Token，自动从同目录下的 .env 读取
      - TUSHARE_TOKEN=${TUSHARE_TOKEN}
    volumes:
      # 挂载宿主机目录，容器重建/更新后数据不丢失
      - ./data:/app/national_team_radar/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1:8765/api/status"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
```

### 3. 配置环境变量 `.env`
在 `/opt/ever_quant/` 下创建 `.env` 文件，填入您的真实 Tushare Token：
```bash
cat << 'EOF' > /opt/ever_quant/.env
TUSHARE_TOKEN=1952633a615e042916f894d79376491f6cecbeb44f77b4effc896301
EOF

# 保护环境变量文件权限
chmod 600 /opt/ever_quant/.env
```

### 4. 启动容器
```bash
# 拉取最新镜像并后台启动
docker compose pull
docker compose up -d

# 查看运行状态
docker compose ps
docker compose logs -f
```

---

## 第三步：配置 Nginx 域名反向代理

假设您分配的二级域名为 **`radar.sensen.li`**（或其它 `*.sensen.li`）：

### 1. 配置 DNS 解析
在您的域名 DNS 控制台添加 A 记录：
- 主机记录：`radar`（或 `*.sensen.li`）
- 记录值：服务器 `tjsj.sensen.li` 对应的公网 IP

### 2. 添加 Nginx 站点配置
将以下配置放入 `/etc/nginx/conf.d/radar.sensen.li.conf`：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name radar.sensen.li;

    # Let's Encrypt 证书申请验证目录
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # 自动重定向至 HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name radar.sensen.li;

    # SSL 证书配置（使用 Certbot 生成后自动填入）
    ssl_certificate /etc/letsencrypt/live/radar.sensen.li/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/radar.sensen.li/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Gzip 静态资源压缩
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_proxied any;
    gzip_types text/plain text/css application/json application/javascript image/svg+xml;

    location / {
        proxy_pass http://127.0.0.1:8765;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket & SSE 长连接流式支持
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # 禁用缓冲确保实时推送
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

### 3. 一键申请免费 HTTPS 证书（Certbot）
```bash
certbot --nginx -d radar.sensen.li
```
测试并重载 Nginx：
```bash
nginx -t && nginx -s reload
```

---

## 第四步：后续更新与维护命令

以后代码更新后，GitHub Actions 会自动构建并发布新版本镜像。服务器上仅需执行两行命令即可完成平滑热升级：

```bash
cd /opt/ever_quant
docker compose pull
docker compose up -d
```
挂载在 `/opt/ever_quant/data` 的历史缓存与行情数据将完整保留。
