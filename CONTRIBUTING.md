# Contributing to SyntropyBridge

Thank you for your interest in contributing! This document will guide you through the process.

## 向 SyntropyBridge 贡献

感谢你的贡献兴趣！本文档将引导你完成整个流程。

---

## Development Setup / 开发环境

1. Fork and clone the repository
2. Install Python dependencies: `pip install -r requirements.txt`
3. Install frontend dependencies: `cd frontend && npm ci`
4. Copy `.env.example` to `.env` and configure
5. Run backend: `python -m uvicorn backend.main:app --reload`
6. Run frontend dev server: `cd frontend && npm run dev`

---

## Before Submitting / 提交前

- Run backend tests: `pytest backend/tests/`
- Build frontend: `cd frontend && npm run build`
- Ensure no secrets are committed (check `git status` for `.env`, `*.db`, `*.log`)

---

## Commit Message Format / Commit 格式

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `security`

---

## Code of Conduct / 行为准则

Be respectful, constructive, and inclusive. Harassment or discrimination will not be tolerated.

请保持尊重、建设性和包容。不容忍骚扰或歧视行为。

---

## Questions? / 有问题？

Open a GitHub Discussion or Issue.
