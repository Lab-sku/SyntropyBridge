# Changelog / 更新日志

All notable changes to this project will be documented in this file.

本项目所有重要变更都将记录在此文件中。

---

## [Unreleased]

### Added / 新增

- Production-ready multi-provider AI API gateway
- OpenAI-compatible `/v1/chat/completions`, `/v1/completions`, `/v1/models`
- 13+ built-in providers: OpenAI, Anthropic, Google, MiniMax, DeepSeek, Moonshot, Zhipu, Aliyun, Doubao, NVIDIA, OpenRouter, SiliconFlow, MiMo
- Credit wallet with Stripe + USDT (NOWPayments) payments
- Subscription lifecycle: upgrade, downgrade, cancel, renew
- Web admin dashboard built with React 18 + Vite + Tailwind
- 6-dimensional quota gate (5h/week/month/RPM/TPM/budget)
- Token reservations to prevent concurrent double-spend
- Channel key rotation and circuit breaker
- Audit logs and usage analytics
- Multi-language documentation (English + 中文)

### Security / 安全

- CSRF protection on all state-changing billing endpoints
- IDOR protection on subscription lifecycle endpoints
- Super-admin gate on API key reveal
- HMAC webhook signature verification
- Session binding to User-Agent
- Brute-force lockout protection
- Fernet encryption for stored API keys
- Log redaction for secrets and PII

---

## [0.1.0] - 2026-07-06

### Added / 新增

- Initial public release with complete README, LICENSE, and contribution guidelines.
