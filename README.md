<div align="center">
  <img src="frontend/public/icon.svg" width="128" alt="SyntropyBridge Logo" />
  <h1>SyntropyBridge</h1>
  <p><strong>Unified OpenAI-compatible gateway for 13+ AI providers</strong></p>
  <p>One API key. Multiple models. Built-in billing, quotas & subscriptions.</p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python 3.10+" />
    <img src="https://img.shields.io/badge/React-18-61DAFB?logo=react" alt="React 18" />
    <img src="https://img.shields.io/badge/FastAPI-0.109%2B-009688?logo=fastapi" alt="FastAPI" />
    <img src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker" alt="Docker" />
  </p>

  <p>
    <a href="#english">English</a> •
    <a href="#中文">中文</a> •
    <a href="#quick-start">Quick Start</a> •
    <a href="#deployment">Deployment</a> •
    <a href="#api-documentation">API Docs</a>
  </p>
</div>

---

<a name="english"></a>

# 🇬🇧 English

## 🚀 What is SyntropyBridge?

**SyntropyBridge** is a production-ready, multi-provider AI API relay and monetization platform. It exposes a single **OpenAI-compatible `/v1/chat/completions`** endpoint while routing requests across 13+ upstream model providers, including OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Moonshot/Kimi, MiniMax, Zhipu GLM, Aliyun DashScope, ByteDance Doubao, NVIDIA NIM, OpenRouter, SiliconFlow, and MiMo.

Originally built as a MiniMax proxy, it has evolved into a full-stack SaaS toolkit for operators who want to:

- Resell or internally govern AI model access
- Enforce per-user quotas, budgets, and rate limits
- Charge users via a credit wallet with Stripe and USDT (NOWPayments) integration
- Manage subscriptions, promo codes, redeem codes, and audit logs from a web dashboard

Whether you are running a small team AI hub or a public API platform, SyntropyBridge gives you the routing, billing, and observability layers out of the box.

## ✨ Why SyntropyBridge?

| Pain Point | How SyntropyBridge Solves It |
|-------------|-------------------------------|
| Every provider has a different API | One OpenAI-compatible gateway for all models |
| Hard to bill users per token | Credit wallet + per-request cost tracking |
| No quota/budget control | 6-dimensional quota gate (5h/week/month/RPM/TPM/budget) |
| Single point of failure | Weighted channel rotation + circuit breaker + fallback |
| Webhook delivery is unreliable | Daily reconciliation jobs for Stripe & USDT |
| No admin visibility | Web dashboard for users, orders, providers, pricing, and audit logs |

## 🎨 Features

### Core Gateway

- **13+ Provider Aggregation**: OpenAI, Anthropic, Google, MiniMax, DeepSeek, Moonshot, Zhipu, Aliyun, Doubao, NVIDIA, OpenRouter, SiliconFlow, MiMo
- **OpenAI-Compatible API**: `/v1/models`, `/v1/chat/completions`, `/v1/completions` (streaming + non-streaming)
- **Custom Providers**: Dynamically register any OpenAI-compatible endpoint with SSRF protection
- **Channel Key Rotation**: Multiple keys per provider, weighted round-robin, automatic cooldown on failure
- **Circuit Breaker**: Per-provider failure isolation (5-failure threshold, 30s cooldown)

### User & Access Control

- Session-cookie auth with CSRF protection
- API key authentication (`Authorization: Bearer ...` / `X-API-Key`)
- Per-user API tokens (`mmx_tk_*`) with model/IP restrictions
- Server-side sessions with sliding-window refresh and UA binding
- Brute-force lockout protection

### Billing & Monetization

- **Credit System**: 1 CNY = 100 credits (roughly 1 USD ~ 700 credits)
- **Wallet Ledger**: Atomic balance updates with transaction history
- **Subscription Plans**: free/basic/pro/team/enterprise tiers with monthly credits
- **Top-up Orders**: Stripe Checkout + USDT (NOWPayments) + admin manual grants
- **Promo & Redeem Codes**: Discount/bonus/credits/plan-days campaigns
- **Per-Credit Expiration**: Optional TTL on credit entries

### Quotas & Reliability

- 6-dimensional quota gate: 5h window, weekly, monthly, monthly budget, RPM, TPM
- Per-request token reservations to prevent concurrent double-spend
- SQLite-backed idempotency store (24h retention) for SDK retries
- Daily/hourly background workers for subscription lifecycle, credits sweep, reservation TTL, and reconciliation

### Admin & Observability

- React 18 + Vite + Tailwind admin dashboard
- Role-based admin with super-admin gate
- Audit logs for every sensitive operation
- Usage analytics: daily/monthly, by model/provider, top users, CSV export
- Provider health metrics: latency p50/p95, success rate
- In-app notifications and low-balance banners

## 🏗️ Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   React SPA     │────▶│  FastAPI (1 worker)│────▶│  SQLite (WAL)   │
│  (Admin + Chat) │     │  - auth/routes     │     │  users/wallets  │
└─────────────────┘     │  - billing/quota   │     │  usage/orders   │
                        │  - proxy/providers │     └─────────────────┘
                        └────────┬─────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
        ▼                        ▼                        ▼
   ┌─────────┐            ┌──────────┐           ┌─────────────┐
   │ OpenAI  │            │ Anthropic│           │   DeepSeek  │
   └─────────┘            └──────────┘           └─────────────┘
   ┌─────────┐            ┌──────────┐           ┌─────────────┐
   │  Google │            │ MiniMax  │           │  Moonshot   │
   └─────────┘            └──────────┘           └─────────────┘
   ┌─────────┐            ┌──────────┐           ┌─────────────┐
   │  Zhipu  │            │  Aliyun  │           │    ...      │
   └─────────┘            └──────────┘           └─────────────┘
```

> SQLite is intentionally chosen for cost-sensitive, single-node SaaS deployments. The architecture assumes **one Uvicorn worker** to avoid database lock contention.

## 📸 Screenshots

> Screenshots will be added here. Place them in `docs/screenshots/` and reference them with relative paths.

| Admin Dashboard | User Wallet | Provider Health |
|-----------------|-------------|-----------------|
| ![dashboard](docs/screenshots/dashboard.png) | ![wallet](docs/screenshots/wallet.png) | ![health](docs/screenshots/health.png) |

## 🛠️ Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.10+, FastAPI, Uvicorn, SQLite (WAL) |
| Frontend | React 18, Vite 5, Tailwind CSS, Zustand, i18next |
| HTTP Client | httpx (async, connection pooling) |
| Crypto | cryptography (Fernet), PBKDF2-HMAC-SHA256 |
| Payments | Stripe, NOWPayments (USDT) |
| Deployment | Docker, Docker Compose, systemd, Nginx |
| Testing | pytest, temp SQLite DB |

<a name="quick-start"></a>
## ⚡ Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
cd YOUR_REPO_NAME

# 2. Configure production environment
cp deploy/.env.production.example .env.production
# Edit .env.production and fill SECRET_KEY, ENCRYPTION_KEY, ADMIN_PASSWORD, provider keys

# 3. Build and start everything
docker-compose up -d --build

# 4. Visit http://localhost:8000 and create the admin account
#    (or set ADMIN_PASSWORD in .env.production for auto-creation)
```

### Option 2: Local Development

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install frontend dependencies and build
cd frontend
npm ci
npm run build
cd ..

# 3. Configure environment
cp .env.example .env
# Edit .env

# 4. Start backend with auto-reload
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 5. Or simply double-click start.bat on Windows
```

## ⚙️ Configuration

Copy `.env.example` to `.env` and fill in the required values:

```bash
# Security (generate with secrets.token_urlsafe)
SECRET_KEY=your-secret-key
ENCRYPTION_KEY=your-fernet-key

# Admin bootstrap
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-strong-password

# At least one provider key
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=...
```

> **Never commit `.env` or `*.db` files.** They are already ignored by `.gitignore`.

<a name="api-documentation"></a>
## 📖 API Documentation

Once running, interactive docs are available at:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Example: Chat Completions

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <user_api_key>" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

### Example: Admin Login

```bash
curl -X POST http://localhost:8000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-password"}'
```

<a name="deployment"></a>
## 🚀 Deployment

See [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md) for the full production runbook, including:

- systemd service + timer setup
- Nginx reverse proxy with SSE support
- SQLite backup script
- Permission-aware health probes

### Bare Metal Quick Checklist

1. Copy `deploy/.env.production.example` to `.env.production`
2. Generate strong `SECRET_KEY` and `ENCRYPTION_KEY`
3. Restrict `.env.production` permissions: `chmod 600 .env.production`
4. Point `DATABASE_PATH` to `/var/lib/syntropybridge/data.db`
5. Run daily + hourly workers via systemd timers
6. Put Nginx in front for HTTPS and rate limiting

## 🔁 Background Workers

| Worker | Frequency | Responsibilities |
|--------|-----------|------------------|
| Daily | Once per day | Subscription expiry/renewal, soft-delete purge, credits sweep, Stripe/USDT reconciliation |
| Hourly | Every hour | Expired orders, pending payments, upcoming renewals, reservation TTL sweep |

Run manually:

```bash
python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_daily_jobs()"
python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_hourly_jobs()"
```

## 🔒 Security Highlights

- CSRF triple-compare on all state-changing billing endpoints
- IDOR protection on subscription lifecycle endpoints
- Super-admin gate on API key reveal
- HMAC webhook signature verification for Stripe and USDT
- Server-side session binding to User-Agent
- Password policy: 12+ chars, 3 of 4 character classes
- Brute-force lockout after 8 failures in 15 minutes
- API keys and provider keys encrypted at rest with Fernet
- Log redaction for secrets, emails, and PII

## 🗺️ Roadmap

- [x] Multi-provider OpenAI-compatible gateway
- [x] Credit wallet + Stripe + USDT payments
- [x] Subscription lifecycle (upgrade/downgrade/cancel/renew)
- [x] Admin dashboard with audit logs
- [x] Channel rotation and circuit breaker
- [ ] Real-time usage WebSocket dashboard
- [ ] Multi-language model fine-tuning marketplace
- [ ] Grafana/Prometheus metrics exporter
- [ ] OpenID Connect / SSO integration
- [ ] Multi-region deployment guide

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Run tests: `pytest backend/tests/`
4. Build frontend: `cd frontend && npm run build`
5. Commit with clear messages
6. Open a Pull Request

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/) and [React](https://react.dev/)
- Provider logos are trademarks of their respective owners

---

<a name="中文"></a>

# 🇨🇳 中文

## 🚀 项目简介

**SyntropyBridge** 是一个生产级的多 Provider AI API 中转与商业化平台。它对外暴露统一的 **OpenAI 兼容 `/v1/chat/completions`** 接口，后端自动路由到 13+ 家上游模型供应商，包括 OpenAI、Anthropic Claude、Google Gemini、DeepSeek、Moonshot/Kimi、MiniMax、智谱 GLM、阿里云 DashScope、字节豆包、NVIDIA NIM、OpenRouter、SiliconFlow、MiMo。

项目最初是一个 MiniMax 代理，现已发展为完整的一站式 SaaS 工具，适合以下场景：

- 对外转售 AI 模型 API 能力
- 企业内部统一管理和计费 AI 调用
- 按用户/团队设置配额、预算、速率限制
- 通过 Stripe、USDT（NOWPayments）充值积分，管理订阅、兑换码、审计日志

无论你是运营一个小型团队 AI 门户，还是公开 API 平台，SyntropyBridge 都能提供开箱即用的路由、计费和可观测能力。

## ✨ 为什么选择 SyntropyBridge？

| 痛点 | SyntropyBridge 的解决方案 |
|------|---------------------------|
| 每家厂商 API 不统一 | 一个 OpenAI 兼容网关对接所有模型 |
| 难以按 token 计费 | 积分钱包 + 每次请求成本追踪 |
| 缺少配额/预算控制 | 6 维配额门控（5小时/周/月/RPM/TPM/预算） |
| 单点故障风险 | 加权渠道轮询 + 熔断器 + 自动降级 |
| Webhook 可能丢消息 | Stripe / USDT 每日对账任务 |
| 缺少管理可视化 | Web 后台管理用户、订单、渠道、定价、审计日志 |

## 🎨 核心特性

### 统一网关

- **13+ 模型供应商聚合**：OpenAI、Anthropic、Google、MiniMax、DeepSeek、Moonshot、智谱、阿里云、豆包、NVIDIA、OpenRouter、SiliconFlow、MiMo
- **OpenAI 兼容 API**：`/v1/models`、`/v1/chat/completions`、`/v1/completions`（流式 + 非流式）
- **自定义 Provider**：动态注册任意 OpenAI 兼容端点，内置 SSRF 防护
- **渠道密钥轮询**：每个供应商支持多密钥，加权选择，失败自动冷却
- **熔断器**：按供应商隔离故障（5 次失败阈值，30 秒冷却）

### 用户与权限

- 基于 Session Cookie 的身份验证 + CSRF 防护
- API Key 认证（`Authorization: Bearer ...` / `X-API-Key`）
- 用户级 API Token（`mmx_tk_*`）支持模型/IP 限制
- 服务端 Session 滑动刷新 + UA 绑定
- 暴力破解锁定保护

### 计费与商业化

- **积分体系**：1 元人民币 = 100 积分（约 1 美元 ~ 700 积分）
- **钱包账本**：原子化余额更新 + 交易历史
- **订阅套餐**：免费版/基础版/专业版/团队版/企业版，含月度积分
- **充值订单**：Stripe Checkout + USDT（NOWPayments）+ 管理员手动赠金
- **优惠码与兑换码**：折扣/赠金/积分/套餐天数活动
- **积分过期**：支持为积分条目设置 TTL

### 配额与可靠性

- 6 维配额门控：5 小时窗口、周、月、月度预算、RPM、TPM
- 每次请求的 token 预留机制，防止并发超刷
- SQLite 幂等存储（24 小时保留），防止 SDK 重试重复扣费
- 日/小时后台任务：订阅生命周期、积分过期清理、预留 TTL、对账

### 后台与可观测性

- React 18 + Vite + Tailwind 管理后台
- 基于角色的管理员 + 超级管理员权限门控
- 敏感操作审计日志
- 用量分析：日/月统计、按模型/供应商、Top 用户、CSV 导出
- 供应商健康指标：p50/p95 延迟、成功率
- 应用内通知与低余额横幅

## 🏗️ 系统架构

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   React SPA     │────▶│  FastAPI (单 worker)│────▶│  SQLite (WAL)   │
│  (管理后台+聊天) │     │  - 认证/路由      │     │  用户/钱包      │
└─────────────────┘     │  - 计费/配额      │     │  用量/订单      │
                        │  - 代理/供应商    │     └─────────────────┘
                        └────────┬─────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
        ▼                        ▼                        ▼
   ┌─────────┐            ┌──────────┐           ┌─────────────┐
   │ OpenAI  │            │ Anthropic│           │   DeepSeek  │
   └─────────┘            └──────────┘           └─────────────┘
   ┌─────────┐            ┌──────────┐           ┌─────────────┐
   │  Google │            │ MiniMax  │           │  Moonshot   │
   └─────────┘            └──────────┘           └─────────────┘
   ┌─────────┐            ┌──────────┐           ┌─────────────┐
   │  智谱    │            │  阿里云  │           │    ...      │
   └─────────┘            └──────────┘           └─────────────┘
```

> 特意选择 SQLite 用于成本敏感的单节点 SaaS 场景。为避免数据库锁争用，架构要求 **单 Uvicorn worker** 运行。

## 📸 界面预览

> 截图将放在这里。请将图片放入 `docs/screenshots/` 目录，并用相对路径引用。

| 管理后台 | 用户钱包 | 供应商健康 |
|---------|---------|-----------|
| ![dashboard](docs/screenshots/dashboard.png) | ![wallet](docs/screenshots/wallet.png) | ![health](docs/screenshots/health.png) |

## 🛠️ 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.10+, FastAPI, Uvicorn, SQLite (WAL) |
| 前端 | React 18, Vite 5, Tailwind CSS, Zustand, i18next |
| HTTP 客户端 | httpx（异步连接池） |
| 加密 | cryptography (Fernet), PBKDF2-HMAC-SHA256 |
| 支付 | Stripe, NOWPayments (USDT) |
| 部署 | Docker, Docker Compose, systemd, Nginx |
| 测试 | pytest, 临时 SQLite 数据库 |

## ⚡ 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
cd YOUR_REPO_NAME

# 2. 配置生产环境变量
cp deploy/.env.production.example .env.production
# 编辑 .env.production，填写 SECRET_KEY、ENCRYPTION_KEY、ADMIN_PASSWORD、供应商密钥

# 3. 构建并一键启动
docker-compose up -d --build

# 4. 访问 http://localhost:8000，创建管理员账号
#    （或在 .env.production 中设置 ADMIN_PASSWORD 自动创建）
```

### 方式二：本地开发

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 安装前端依赖并构建
cd frontend
npm ci
npm run build
cd ..

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env

# 4. 启动后端（热重载）
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 5. Windows 用户也可以直接双击 start.bat
```

## ⚙️ 配置说明

复制 `.env.example` 为 `.env`，然后填写必填项：

```bash
# 安全密钥（用 secrets.token_urlsafe 生成）
SECRET_KEY=your-secret-key
ENCRYPTION_KEY=your-fernet-key

# 管理员初始账号
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-strong-password

# 至少配置一个供应商密钥
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=...
```

> **切勿提交 `.env` 或 `*.db` 文件。** 它们已被 `.gitignore` 排除。

## 📖 API 文档

服务启动后，可访问交互式文档：

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### 示例：聊天补全

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <用户_API_Key>" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": false
  }'
```

### 示例：管理员登录

```bash
curl -X POST http://localhost:8000/api/admin/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"你的密码"}'
```

## 🚀 部署指南

详见 [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md)，包含：

- systemd 服务 + 定时任务配置
- Nginx 反向代理（支持 SSE）
- SQLite 备份脚本
- 权限感知的健康检查探针

### 裸机部署速查

1. 复制 `deploy/.env.production.example` 为 `.env.production`
2. 生成强 `SECRET_KEY` 和 `ENCRYPTION_KEY`
3. 限制配置文件权限：`chmod 600 .env.production`
4. 将 `DATABASE_PATH` 指向 `/var/lib/syntropybridge/data.db`
5. 通过 systemd timer 运行日/小时任务
6. 前置 Nginx 提供 HTTPS 和限流

## 🔁 后台任务

| 任务 | 频率 | 职责 |
|------|------|------|
| 每日任务 | 每天一次 | 订阅过期/续费、软删除清理、积分过期、Stripe/USDT 对账 |
| 每小时任务 | 每小时 | 过期订单、待支付订单、即将续费提醒、预留 TTL 清理 |

手动执行：

```bash
python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_daily_jobs()"
python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_hourly_jobs()"
```

## 🔒 安全亮点

- 所有计费写操作启用 CSRF 三重校验
- 订阅生命周期接口增加 IDOR 防护
- API Key 明文查看仅限超级管理员
- Stripe / USDT Webhook 使用 HMAC 签名验证
- 服务端 Session 绑定 User-Agent
- 密码策略：12 位以上，4 类字符中至少 3 类
- 15 分钟内 8 次失败触发暴力破解锁定
- API Key 和供应商密钥使用 Fernet 加密存储
- 日志自动脱敏（密钥、JWT、邮箱、PII）

## 🗺️ 路线图

- [x] 多供应商 OpenAI 兼容网关
- [x] 积分钱包 + Stripe + USDT 支付
- [x] 订阅生命周期（升级/降级/取消/续费）
- [x] 带审计日志的管理后台
- [x] 渠道轮询与熔断器
- [ ] 实时用量 WebSocket 仪表盘
- [ ] 多语言模型微调市场
- [ ] Grafana/Prometheus 指标导出
- [ ] OpenID Connect / SSO 集成
- [ ] 多区域部署指南

## 🤝 贡献指南

欢迎贡献！请按以下步骤：

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feature/amazing-feature`
3. 运行测试：`pytest backend/tests/`
4. 构建前端：`cd frontend && npm run build`
5. 提交清晰的 commit message
6. 发起 Pull Request

## 📄 开源协议

本项目采用 MIT 协议 — 详见 [LICENSE](LICENSE) 文件。

## 🙏 致谢

- 基于 [FastAPI](https://fastapi.tiangolo.com/) 和 [React](https://react.dev/) 构建
- 各供应商 Logo 归其所有者所有

---

## 🔐 Security & Privacy Notice / 安全与隐私说明

- **Never commit `.env`, `*.db`, `*.log`, SSL certificates, or any file containing secrets.**
- **Production deployments must set strong `SECRET_KEY`, `ENCRYPTION_KEY`, and non-`*`** `CORS_ORIGINS`.
- The repository intentionally excludes runtime data. If you see `minimax_proxy.db` or `.env` in `git status`, do not add them.

- **切勿提交 `.env`、`*.db`、`*.log`、SSL 证书或任何包含密钥的文件。**
- **生产环境必须设置强 `SECRET_KEY`、`ENCRYPTION_KEY`，且 `CORS_ORIGINS` 不能为 `*`。**
- 本仓库默认排除运行时数据。如果 `git status` 中出现 `minimax_proxy.db` 或 `.env`，请勿加入版本控制。

## 💬 Support / 支持

- English: Open a GitHub Issue
- 中文：提交 GitHub Issue 或使用 Discussions
