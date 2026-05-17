# Txxx 公益站 — AI 生图平台

免费 AI 图片生成平台，基于 ChatGPT 账号池实现，支持 PWA 离线访问、多用户管理、异步生图任务。

## 功能一览

| 功能 | 说明 |
|------|------|
| 🎨 **AI 生图** | 基于 ChatGPT 账号池下发图片生成任务，支持同步/异步两种接口 |
| 🖼️ **图片管理** | 用户个人的生成历史管理 |
| 🎯 **提示词画廊** | 公开分享 prompt，供他人借鉴 |
| 🔐 **用户系统** | 注册/登录、JWT 鉴权、GitHub/Google OAuth、邮箱验证 |
| 👑 **管理后台** | ChatGPT 账号池管理、注册机配置、用户管理 |
| 📱 **PWA 支持** | 可安装到手机桌面，离线可用 |
| 💬 **对话记录** | 生图参数、对话上下文自动保存 |

## 项目结构

```
├── 前端/                  # Vue.js 构建产物（SPA + PWA）
│   ├── assets/           # 编译后的 JS/CSS
│   ├── index.html        # 入口页
│   ├── manifest.json     # PWA 清单
│   └── sw.js             # Service Worker
├── 后端/                  # FastAPI 后端
│   ├── app/
│   │   ├── main.py              # 应用入口 & 路由注册
│   │   ├── config.py            # 配置（pydantic-settings）
│   │   ├── database.py          # SQLAlchemy 异步引擎
│   │   ├── models/              # 数据模型
│   │   ├── schemas/             # Pydantic 请求/响应
│   │   ├── routers/             # API 路由
│   │   ├── services/            # 业务逻辑
│   │   │   ├── auth.py          # JWT / OAuth
│   │   │   ├── email.py         # 邮件发送
│   │   │   ├── oauth.py         # OAuth 逻辑
│   │   │   ├── verification.py  # 邮箱验证码
│   │   │   └── gpt_pool/        # ChatGPT 账号池引擎
│   │   └── middleware/
│   │       └── cors.py          # CORS 中间件
│   ├── alembic/                 # 数据库迁移
│   ├── requirements.txt
│   ├── .env.example             # 环境变量模板
│   └── uploads/                 # 用户上传参考图
├── scripts/
│   └── build-icons.py
└── .gitignore
```

## 快速启动

### 1. 环境要求

- Python ≥ 3.10
- 可选：Chromium（用于 ChatGPT 账号自动注册）

### 2. 安装依赖

```bash
cd 后端
pip install -r requirements.txt

# 如需账号注册机功能，额外安装 Playwright 浏览器
playwright install chromium
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，至少修改：

| 变量 | 说明 |
|------|------|
| `SECRET_KEY` | JWT 密钥，改为随机 32 位以上字符串 |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth（可选） |
| `SMTP_*` | 邮箱配置（注册验证码，开发时可关掉 `REQUIRE_EMAIL_VERIFICATION=false`） |

### 4. 运行

```bash
# 开发模式（热重载）
uvicorn app.main:app --reload --port 8088

# 生产模式
uvicorn app.main:app --host 0.0.0.0 --port 8088
```

前端已构建为静态文件，需要配 Nginx 一并托管：

```nginx
# 前端 SPA
location / {
    root /path/to/前端;
    try_files $uri $uri/ /index.html;
}

# 后端 API 反代
location /api/ {
    proxy_pass http://127.0.0.1:8088;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}

# 用户上传 / 生图产物
location /uploads/ { alias /path/to/后端/uploads/; }
location /images/  { alias /path/to/后端/data/images/; }
```

也可参考 `前端/nginx-pwa.conf.snippet`。

### 5. 数据库迁移

首次启动自动建表。后续表结构变更：

```bash
cd 后端
alembic revision --autogenerate -m "描述"
alembic upgrade head
```

## API 概览

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/auth/register` | POST | 用户注册 |
| `/api/auth/login` | POST | 登录获取 JWT |
| `/api/auth/github` | GET | GitHub OAuth 跳转 |
| `/api/auth/google` | GET | Google OAuth 跳转 |
| `/api/generate` | POST | 同步生图 |
| `/api/generate/async` | POST | 异步提交生图任务 |
| `/api/generate/task/{id}` | GET | 查询异步任务状态 |
| `/api/images` | GET | 用户图片列表 |
| `/api/gallery` | GET | 公开提示词画廊 |
| `/api/conversations` | GET | 对话历史 |
| `/api/feedback` | POST | 提交反馈 |
| `/api/admin/accounts` | GET | 管理 ChatGPT 账号池 |
| `/api/admin/register` | POST | 触发自动注册 |
| `/api/health` | GET | 健康检查 |

## 技术栈

- **后端**：Python 3.10+ / FastAPI / SQLAlchemy (asyncio) / SQLite / Alembic
- **前端**：Vue.js 3 (built) / PWA / Service Worker
- **鉴权**：JWT (python-jose) / GitHub OAuth / Google OAuth
- **生图引擎**：基于 curl-cffi 模拟 ChatGPT 网页端请求，支持账号池轮换
- **注册机**：Playwright 自动化注册 ChatGPT 账号（备选）

## 许可证

本项目仅供学习研究使用。
