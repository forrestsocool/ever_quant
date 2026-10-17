# EverQuant 服务器部署与运维手册 (`tjsj.sensen.li`)

本手册记录了在服务器 **`tjsj.sensen.li`** 的 `/opt/ever_quant` 路径下使用 Docker Compose 部署，并通过 Nginx SNI 流分流与通配符 SSL 证书反向代理接入域名的完整架构与实操记录。

---

## 一、服务器环境与网络拓扑

```
[ 客户端浏览器 / AI Agent ]
             │ (HTTPS 443 / 域名 radar.sensen.li)
             ▼
[ Nginx SNI 443 端口流分流 (stream map) ]
             │ (根据 SNI 匹配 radar.sensen.li)
             ▼
[ Nginx 本地 8043 端口 (HTTP/2 + 通配符 SSL 证书终端) ]
   ├── /etc/certs/sensen.li/fullchain.cer
   └── /etc/certs/sensen.li/sensen.li.key
             │ (反向代理 http://127.0.0.1:8765)
             ▼
[ Docker 容器: ever_quant_radar (ARM64 架构) ]
   ├── 服务端口: 127.0.0.1:8765:8765
   ├── 挂载卷: /opt/ever_quant/data -> /app/national_team_radar/data
   ├── 环境变量: TUSHARE_TOKEN (读取同目录下的 .env)
   └── 定时更新: 工作日每日 09:00 与 18:30 自动抓取更新
```

- **服务器操作系统**：Ubuntu 24.04 LTS (Oracle Linux 内核)
- **CPU 架构**：**`ARM64 / aarch64`**
- **公网 IP**：`152.67.224.226`
- **项目路径**：`/opt/ever_quant`
- **容器名称**：`ever_quant_radar`
- **接入域名**：`radar.sensen.li`

---

## 二、当前部署状态（已完成）

已在服务器完成以下全部配置并正常运行：

1. **源码与运行目录**：
   - Git 仓库已克隆至 `/opt/ever_quant`。
   - 数据目录 `/opt/ever_quant/data` 已挂载，ETF 份额、净值、行情、快照数据均在宿主机持久化。
2. **环境变量配置**：
   - `/opt/ever_quant/.env` 已创建并配置真实 Tushare Token（权限设为 `chmod 600`）。
3. **ARM64 原生 Docker 镜像**：
   - Docker 容器已基于 ARM64 本地完成构建并由 Docker Compose 启动。
   - GitHub Actions 工作流（`.github/workflows/docker-publish.yml`）已升级为 `platforms: linux/amd64,linux/arm64` 双架构自动打包。
4. **Nginx 反代与证书配置**：
   - 站点配置 `/etc/nginx/conf.d/radar.sensen.li.conf` 已加载。
   - `/etc/nginx/nginx.conf` 中的 `stream { map $ssl_preread_server_name $name }` 已添加 `radar.sensen.li 127.0.0.1:8043;`。
   - 使用系统已有的 `*.sensen.li` ECC 通配符证书，自动支持 HTTPS/2 与 WebSocket/SSE 流式推送。

---

## 三、用户操作（仅需一步：DNS 解析）

服务已经在服务器内部监听并健康运行。您只需在域名的 DNS 服务商（如 Cloudflare、腾讯云、阿里云等）处添加一条解析记录：

- **主机记录**：`radar`（对应 `radar.sensen.li`）
- **记录类型**：`A`
- **记录值**：`152.67.224.226`（或者 `CNAME` 指向 `tjsj.sensen.li`）

> **生效测试**：
> DNS 解析生效后，直接在浏览器中打开：
> **`https://radar.sensen.li`**
> 即可看到宽基资金脉搏与沪深300双轴雷达面板！

---

## 四、常用日常运维命令

直接免密登录服务器：
```bash
ssh ubuntu@tjsj.sensen.li
```

### 1. 查看容器运行状态
```bash
cd /opt/ever_quant
sudo docker compose ps
```

### 2. 查看容器实时日志
```bash
sudo docker compose logs -f
```

### 3. 查看实时健康状态与数据快照
```bash
# 检查后端状态
curl -s http://127.0.0.1:8765/api/status

# 检查当前数据快照
curl -s http://127.0.0.1:8765/api/snapshot | head -c 200
```

### 4. 代码更新与平滑重启
当 GitHub 仓库有新代码时：
```bash
cd /opt/ever_quant
git pull
sudo docker compose restart
```
所有位于 `/opt/ever_quant/data` 的历史行情与快照缓存将完整保留，无需重新冷启动抓取。
