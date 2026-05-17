"""图片生成服务的薄封装层。

负责的事：
    1. 从账号池选号
    2. 调用 chatgpt2api 移植过来的 openai_v1_image_generations.handle
    3. 失败按类型重试，最多 MAX_GENERATION_RETRIES 次
    4. 把成功/失败结果回写到账号池（mark_image_result）

所有调用都是同步的；外部异步路由通过 run_in_threadpool 调用本模块。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.gpt_pool import MAX_GENERATION_RETRIES
from app.services.gpt_pool.account_service import account_service
from app.services.gpt_pool.helper import anonymize_token

logger = logging.getLogger(__name__)


# ===== 异常体系：路由层据此映射 HTTP 状态码 =====
class PoolExhaustedError(RuntimeError):
    """账号池为空或所有账号都不可用 → 路由 503。"""


class GenerationFailedError(RuntimeError):
    """重试后仍失败的远程错误 → 路由 502。"""


@dataclass
class GenerationResult:
    image_url: str
    account_id: int | None
    account_quota_after: int | None
    raw: dict


def _extract_image_url(raw: dict) -> str | None:
    """从 OpenAI 兼容响应里取一张图的 URL（site 当前每次只生成 1 张）。"""
    if not isinstance(raw, dict):
        return None
    data = raw.get("data")
    if not isinstance(data, list) or not data:
        return None
    item = data[0]
    if not isinstance(item, dict):
        return None
    return item.get("url") or item.get("b64_json")


def generate_one(
    prompt: str,
    *,
    n: int = 1,
    size: str | None = None,
    response_format: str = "url",
    base_url: str | None = None,
) -> GenerationResult:
    """从账号池中选一个账号生成 1 张图（n 当前固定按 1 处理）。

    成功 → 返回 GenerationResult，调用方再扣用户额度
    账号池为空 → 抛 PoolExhaustedError
    重试用尽 → 抛 GenerationFailedError
    """
    # 延迟 import，避免在 site 启动早期触发 chatgpt2api 重型依赖
    from app.services.gpt_pool.openai_backend_api import InvalidAccessTokenError
    from app.services.gpt_pool.protocol.openai_v1_image_generations import handle

    last_error: Exception | None = None

    for attempt in range(MAX_GENERATION_RETRIES + 1):
        # 1. 选号
        try:
            token = account_service.get_available_access_token()
        except RuntimeError as exc:
            # 项目 B 风格：账号池空时抛 RuntimeError("no available image quota")
            raise PoolExhaustedError(str(exc)) from exc

        # 2. 调用
        try:
            body = {
                "prompt": prompt,
                "model": "gpt-image-2",
                "n": max(1, int(n)),
                "size": size,
                "response_format": response_format,
                "base_url": base_url,
                "access_token": token,
            }
            raw = handle(body)
            # handle 在 stream=False 模式下返回 dict
            if not isinstance(raw, dict):
                # 非 stream 路径不应返回 iterator；若返回了说明上游异常
                raise GenerationFailedError("upstream returned non-dict response")

            image_url = _extract_image_url(raw)
            if not image_url:
                # 远程返回错误体（如 {"error": {...}}），按失败处理
                err = raw.get("error") if isinstance(raw, dict) else None
                logger.warning(
                    "image gen no url, account=%s, err=%s",
                    anonymize_token(token),
                    err,
                )
                account_service.mark_image_result(token, success=False)
                last_error = GenerationFailedError(str(err) or "no image returned")
                continue

            # 3. 成功路径：扣账号额度
            account_after = account_service.mark_image_result(token, success=True)
            return GenerationResult(
                image_url=image_url,
                account_id=(account_after or {}).get("id"),
                account_quota_after=(account_after or {}).get("quota"),
                raw=raw,
            )
        except InvalidAccessTokenError as exc:
            # 4. token 失效：自动剔除 + 切下一个
            logger.info(
                "invalid token detected, account=%s; retry attempt=%d",
                anonymize_token(token),
                attempt,
            )
            account_service.remove_invalid_token(token, "image_generation")
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            # 其他异常：记 fail，切下一个
            logger.warning(
                "image gen error, account=%s, attempt=%d, err=%s",
                anonymize_token(token),
                attempt,
                exc,
            )
            account_service.mark_image_result(token, success=False)
            last_error = exc
            continue

    # 重试用尽
    raise GenerationFailedError(f"generation failed after retries: {last_error}")
