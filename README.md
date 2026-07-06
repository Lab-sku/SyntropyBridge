# 中转平台

一个简洁高效的 API 中转与管理平台，支持多用户管理、配额控制、用量统计与多渠道配置。

## 功能特性

- ✅ **多用户支持** - 为每个用户分配独立的 API Key
- ✅ **配额管理** - 支持 5 小时/周配额限制，可手动调整
- ✅ **用量统计** - 实时查看各用户调用次数和 Token 消耗
- ✅ **管理后台** - 简洁美观的 Web 管理界面
- ✅ **跨网络访问** - 部署在服务器上即可公网访问

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 安装前端依赖并构建（推荐）

```bash
cd frontend
npm ci
npm run build
```

### 3. 配置环境变量

```bash
# 复制示例配置
cp .env.example .env
# 然后编辑 .env，至少填：SECRET_KEY / ENCRYPTION_KEY / CORS_ORIGINS
```

### 4. 启动服务

```bash
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

或双击运行 `start.bat`

### 5. 访问管理后台

打开浏览器访问: http://localhost:8000

- 首次启动需要在登录页初始化管理员账号（强口令）

## 使用方法

### 创建用户

1. 登录管理后台
2. 点击「添加用户」按钮
3. 输入用户名，设置配额
4. 保存后会显示该用户的 API Key（仅显示一次）

### API 调用

用户使用分配的 API Key 调用接口：

```bash
curl -X POST http://localhost:8000/v1/text/chatcompletion_v2 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <user_api_key>" \
  -d '{
    "model": "abab5.5",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### 手动调整配额

1. 在管理后台点击用户的「编辑」按钮
2. 修改 5 小时配额或周配额
3. 保存即可

## 项目结构

```
.
├── backend/
│   ├── main.py           # FastAPI 主程序
│   ├── config.py         # 配置文件
│   ├── database.py      # 数据库操作
│   ├── models.py        # 数据模型
│   ├── routes/          # API 路由
│   │   ├── proxy.py     # 代理接口
│   │   └── admin.py     # 管理接口
│   └── services/        # 业务逻辑
│       ├── user_service.py
│       └── proxy_service.py
├── frontend/
│   └── index.html       # 管理后台页面
├── requirements.txt    # Python 依赖
└── start.bat           # 启动脚本
```

## 配额说明

| 配额类型 | 说明 |
|---------|------|
| 5小时配额 | 滚动计算，当前5小时内的调用次数上限 |
| 周配额 | 滚动计算，当前7天内的调用次数上限 |

当任一配额用完时，该用户的请求会被拒绝并返回 429 错误。

## 部署到云服务器

1. 上传代码到服务器
2. 安装依赖: `pip install -r requirements.txt`
3. 配置环境变量:
   ```bash
   export MINIMAX_API_KEY="your_api_key"
   export ADMIN_PASSWORD="your_password"
   ```
4. 使用 systemd 或 pm2 启动服务
5. 配置 Nginx 反向代理（可选，用于 HTTPS）

## 注意事项

- 请妥善保管管理员账号密码
- API Key 创建后只显示一次，请及时保存
- 建议定期备份数据库文件 `minimax_proxy.db`
- 默认情况下 SQLite 数据库文件与项目文件在同一目录
