"""gpt_pool 包：从 chatgpt2api 项目移植的 ChatGPT 图片生成网关组件。

本包对外暴露：
    - account_service: 账号池单例（管理 GptAccount）
    - register_service: 注册机单例（管理 RegisterConfig + RegisterLog）
    - image_generation: 生图调用封装

注意：本包内部使用同步 SQLAlchemy session（见 db_session.py），与 site 主体的
异步 session 解耦。两者通过 fastapi.concurrency.run_in_threadpool 在路由层桥接。
"""
from __future__ import annotations

# 全局常量（design §7）
MAX_GENERATION_RETRIES = 2
# 单次生图等待时间。OpenAI 生图通常 30~90s，给 90s 比较稳。
# 注意：Cloudflare 免费版 origin 响应硬上限是 100s（524 错误），别超过这个数。
GENERATION_TIMEOUT_SECONDS = 90
