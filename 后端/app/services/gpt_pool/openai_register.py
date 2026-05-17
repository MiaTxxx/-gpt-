from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import urllib3
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.services.gpt_pool.account_service import account_service
from app.services.gpt_pool import mail_provider

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 15,  # 优化：减少邮箱请求超时
        "wait_timeout": 30,
        "wait_interval": 0.8,  # 优化：减少验证码轮询间隔，从2秒改为0.8秒
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 5,  # 优化：增加并发线程数，从3改为5
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
except Exception:
    pass

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agents = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
user_agent = random.choice(user_agents)
sec_ch_ua = '"Google Chrome";v="124", "Not?A_Brand";v="8", "Chromium";v="124"'
sec_ch_ua_full_version_list = '"Chromium";v="124.0.6367.60", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="124.0.6367.60"'
default_timeout = 15  # 优化：减少超时时间，从30秒改为15秒
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None

common_headers = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": auth_base,
    "priority": "u=1, i",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9",
    "accept-encoding": "gzip, deflate, br, zstd",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "cache-control": "max-age=0",
    "pragma": "no-cache",
    "dnt": "1",
    "te": "trailers",
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def create_mailbox(username: str | None = None) -> dict:
    return mail_provider.create_mailbox(config["mail"], username)


def wait_for_code(mailbox: dict) -> str | None:
    return mail_provider.wait_for_code(config["mail"], mailbox)


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    generator = SentinelTokenGenerator(device_id, user_agent)
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": user_agent,
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )
    data = _response_json(resp)
    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    return json.dumps({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))


def _is_socks_proxy(proxy: str) -> bool:
    candidate = str(proxy or "").strip().lower()
    return candidate.startswith("socks5://") or candidate.startswith("socks5h://")


def create_session(proxy: str = "") -> Any:
    if _is_socks_proxy(proxy):
        return curl_requests.Session(impersonate="chrome", verify=False, proxy=proxy)
    session = requests.Session()
    retry = Retry(total=2, connect=2, read=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for attempt in range(max(1, retry_attempts)):
        try:
            resp = session.request(method.upper(), url, timeout=default_timeout, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise Exception(f"HTTP {resp.status_code}")
            return resp, ""
        except Exception as error:
            last_error = str(error)
            if attempt < retry_attempts - 1:
                # 优化：减少初始延迟，使用更短的指数退避
                delay = min(0.5 + (1.5 ** attempt) + random.uniform(0, 0.5), 8)
                time.sleep(delay)
    return None, last_error


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def extract_oauth_callback_params_from_consent_session(session: requests.Session, consent_url: str, device_id: str) -> dict[str, str] | None:
    if consent_url.startswith("/"):
        consent_url = f"{auth_base}{consent_url}"
    current_url = consent_url
    for _ in range(10):
        response = session.get(current_url, headers=navigate_headers, verify=False, timeout=30, allow_redirects=False)
        callback_params = extract_oauth_callback_params_from_url(str(response.url)) or extract_oauth_callback_params_from_url(str(response.headers.get("Location") or "").strip())
        if callback_params:
            return callback_params
        location = str(response.headers.get("Location") or "").strip()
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        current_url = f"{auth_base}{location}" if location.startswith("/") else location
    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first_part = raw.split(".")[0]
        padding = 4 - len(first_part) % 4
        if padding != 4:
            first_part += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(first_part))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None
    headers = dict(common_headers)
    headers["referer"] = consent_url
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    ws_resp = session.post(f"{auth_base}/api/accounts/workspace/select", json={"workspace_id": workspace_id}, headers=headers, verify=False, timeout=30, allow_redirects=False)
    callback_params = extract_oauth_callback_params_from_url(str(ws_resp.headers.get("Location") or "").strip())
    if callback_params:
        return callback_params
    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_headers = dict(common_headers)
    org_headers["referer"] = str(ws_data.get("continue_url") or consent_url)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp = session.post(f"{auth_base}/api/accounts/organization/select", json=body, headers=org_headers, verify=False, timeout=30, allow_redirects=False)
    return extract_oauth_callback_params_from_url(str(org_resp.headers.get("Location") or "").strip())


def exchange_platform_tokens(session: requests.Session, device_id: str, code_verifier: str, consent_url: str) -> dict | None:
    callback_params = extract_oauth_callback_params_from_consent_session(session, consent_url, device_id)
    if not callback_params:
        return None
    code = str(callback_params.get("code") or "").strip()
    if not code:
        return None
    resp = create_session(config["proxy"]).post(
        f"{auth_base}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
        },
        verify=False,
        timeout=60,
    )
    data = _response_json(resp)
    if resp.status_code != 200 or not data.get("access_token") or not data.get("refresh_token") or not data.get("id_token"):
        return None
    payload = _decode_jwt_payload(str(data.get("id_token") or "")) or _decode_jwt_payload(str(data.get("access_token") or ""))
    return {
        "email": str(payload.get("email") or "").strip(),
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("id_token") or "").strip(),
    }


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())

    def close(self) -> None:
        self.session.close()

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _platform_authorize(self, email: str, index: int) -> None:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        _, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            raise RuntimeError(error or f"platform_authorize_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "platform authorize 完成")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/user/register", json={"username": email, "password": password}, headers=headers, verify=False)
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/email-otp/send", headers=self._navigate_headers(f"{auth_base}/create-account/password"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "验证码校验完成")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        headers = self._json_headers(f"{auth_base}/about-you")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/create_account", json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "创建账号资料完成")

    def _password_verify_with_retry(self, password: str, email: str, index: int, max_retries: int = 3) -> tuple:
        """
        带重试的密码验证，包含409冲突处理
        
        Args:
            password: 用户密码
            email: 用户邮箱
            index: 任务索引
            max_retries: 最大重试次数
        
        Returns:
            tuple: (resp, error) 响应对象和错误信息
        """
        retry_delay = 1  # 优化：减少重试延迟，从2秒改为1秒
        conflict_retry_count = 0
        max_conflict_retries = 1  # 冲突最多重试1次
        
        for attempt in range(max_retries):
            headers = self._json_headers(f"{auth_base}/log-in/password")
            headers["openai-sentinel-token"] = build_sentinel_token(
                self.session, self.device_id, "password_verify"
            )
            
            resp, error = request_with_local_retry(
                self.session, "post", 
                f"{auth_base}/api/accounts/password/verify", 
                json={"password": password}, 
                headers=headers, 
                allow_redirects=False, 
                verify=False
            )
            
            # 验证成功
            if resp is not None and resp.status_code == 200:
                if attempt > 0:
                    step(index, f"密码验证重试成功 (第 {attempt + 1} 次)", "green")
                return resp, ""
            
            # ===== 409冲突特殊处理 =====
            if resp is not None and resp.status_code == 409:
                data = _response_json(resp)
                error_message = str(data.get("message", "")).lower()
                error_code = str(data.get("error", "")).lower()
                error_details = str(data.get("detail", "")).lower()
                
                # 合并所有错误信息进行匹配
                full_error_text = f"{error_message} {error_code} {error_details}"
                
                step(index, f"409冲突详情: message={error_message}, error={error_code}, detail={error_details}", "yellow")
                
                # 判断冲突类型
                if any(keyword in full_error_text for keyword in ["already exists", "duplicate", "already registered"]):
                    step(index, f"409冲突: 账号已存在，尝试清除状态后重试", "yellow")
                    # 清除可能冲突的状态
                    self.session.cookies.clear(
                        domain=".auth.openai.com", 
                        path="/"
                    )
                    # 重置device_id（可能导致状态冲突）
                    old_device_id = self.device_id
                    self.device_id = str(uuid.uuid4())
                    step(index, f"重置device_id: {old_device_id[:8]}... -> {self.device_id[:8]}...", "yellow")
                    
                    conflict_retry_count += 1
                    if conflict_retry_count <= max_conflict_retries:
                        time.sleep(retry_delay * (2 ** conflict_retry_count))
                        continue
                    else:
                        step(index, "409冲突重试次数已达上限", "red")
                        return resp, error or "password_verify_conflict_max_retries"
                
                elif any(keyword in full_error_text for keyword in ["session", "concurrent", "logged in", "active session", "invalid_state"]):
                    step(index, f"409冲突: 会话失效或状态无效 (invalid_state)", "yellow")
                    # invalid_state 需要重新执行 authorize
                    self.session.cookies.clear()
                    step(index, "标记需要重新执行 authorize 步骤", "yellow")
                    return resp, "REQUIRE_REAUTHORIZE"  # 特殊标记，告知调用者需要重新授权
                        
                elif any(keyword in full_error_text for keyword in ["verification", "state", "otp", "code"]):
                    step(index, f"409冲突: 验证状态冲突，尝试清除状态并重试", "yellow")
                    self.session.cookies.clear(domain=".auth.openai.com", path="/")
                    time.sleep(2)
                    conflict_retry_count += 1
                    if conflict_retry_count <= max_conflict_retries:
                        continue
                    else:
                        return resp, error or "password_verify_state_conflict"
                
                elif any(keyword in full_error_text for keyword in ["locked", "rate limit", "too many", "throttled"]):
                    step(index, f"409冲突: 账户锁定或限流，等待后重试", "yellow")
                    wait_time = retry_delay * (2 ** conflict_retry_count) + random.uniform(0, 2)
                    step(index, f"等待 {wait_time:.1f} 秒后重试", "yellow")
                    time.sleep(wait_time)
                    conflict_retry_count += 1
                    if conflict_retry_count <= max_conflict_retries:
                        continue
                    else:
                        return resp, error or "password_verify_rate_limited"
                
                elif any(keyword in full_error_text for keyword in ["conflict", "already in progress", "pending"]):
                    step(index, f"409冲突: 操作冲突，等待后重试", "yellow")
                    wait_time = retry_delay * (2 ** conflict_retry_count)
                    time.sleep(wait_time)
                    conflict_retry_count += 1
                    if conflict_retry_count <= max_conflict_retries:
                        continue
                    else:
                        return resp, error or "password_verify_operation_conflict"
                
                else:
                    # 未知的409冲突 - 尝试通用的重试策略
                    step(index, f"409冲突: 未知原因，尝试通用解决方案", "yellow")
                    # 清除所有cookies
                    self.session.cookies.clear()
                    # 重置device_id
                    old_device_id = self.device_id
                    self.device_id = str(uuid.uuid4())
                    step(index, f"通用修复: 清除cookies并重置device_id: {old_device_id[:8]}... -> {self.device_id[:8]}...", "yellow")
                    # 增加等待时间
                    wait_time = retry_delay * (2 ** conflict_retry_count) + random.uniform(1, 3)
                    time.sleep(wait_time)
                    
                    conflict_retry_count += 1
                    if conflict_retry_count <= max_conflict_retries + 1:  # 未知冲突多一次重试机会
                        continue
                    else:
                        step(index, f"409冲突: 未知原因重试失败 - {error_message}", "red")
                        return resp, error or f"password_verify_conflict_{resp.status_code}"
            # ===== 409处理结束 =====
            
            # 其他客户端错误（4xx），不重试
            if resp is not None and 400 <= resp.status_code < 500:
                step(index, f"密码验证客户端错误 (HTTP {resp.status_code})，不重试", "red")
                return resp, error or f"password_verify_client_error_{resp.status_code}"
            
            # 服务器错误（5xx）或无响应，准备重试
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                status_code = getattr(resp, 'status_code', 'unknown')
                step(
                    index, 
                    f"密码验证失败 (HTTP {status_code})，{wait_time}秒后重试 ({attempt + 1}/{max_retries})", 
                    "yellow"
                )
                time.sleep(wait_time)
            else:
                step(index, f"密码验证失败，已达最大重试次数 ({max_retries})", "red")
                return resp, error or f"password_verify_http_{getattr(resp, 'status_code', 'unknown')}"
        
        return None, "password_verify_max_retries_exceeded"

    def _login_and_exchange_tokens(self, email: str, password: str, mailbox: dict, index: int) -> dict:
        step(index, "开始独立登录换 token")
        
        max_reauthorize_attempts = 2
        reauthorize_count = 0
        
        while reauthorize_count <= max_reauthorize_attempts:
            code_verifier, code_challenge = _generate_pkce()
            
            params = {
                "issuer": auth_base,
                "client_id": platform_oauth_client_id,
                "audience": platform_oauth_audience,
                "redirect_uri": platform_oauth_redirect_uri,
                "device_id": self.device_id,
                "screen_hint": "login_or_signup",
                "max_age": "0",
                "login_hint": email,
                "scope": "openid profile email offline_access",
                "response_type": "code",
                "response_mode": "query",
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "auth0Client": platform_auth0_client,
            }
            
            resp, error = request_with_local_retry(
                self.session, "get", 
                f"{auth_base}/api/accounts/authorize?{urlencode(params)}", 
                headers=self._navigate_headers(f"{platform_base}/"), 
                allow_redirects=True, 
                verify=False
            )
            if resp is None:
                raise RuntimeError(error or "platform_login_authorize_failed")
            step(index, f"登录 authorize 完成 (尝试 {reauthorize_count + 1})")
            
            # 使用带重试的密码验证
            resp, error = self._password_verify_with_retry(password, email, index, max_retries=2)
            
            # 检测是否需要重新授权
            if error == "REQUIRE_REAUTHORIZE":
                reauthorize_count += 1
                if reauthorize_count <= max_reauthorize_attempts:
                    step(index, f"会话失效，准备重新授权 (第 {reauthorize_count} 次重试)", "yellow")
                    time.sleep(1)  # 优化：减少重新授权等待时间，从2秒改为1秒
                    continue
                else:
                    raise RuntimeError("reauthorize_max_attempts_exceeded")
            
            if resp is None or resp.status_code != 200:
                raise RuntimeError(error or f"password_verify_http_{getattr(resp, 'status_code', 'unknown')}")
            
            # 验证成功，退出循环
            break
        
        step(index, "密码校验完成")
        payload = _response_json(resp)
        continue_url = str(payload.get("continue_url") or "").strip()
        page_type = str(((payload.get("page") or {}).get("type")) or "")
        if page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url:
            step(index, "独立登录需要邮箱验证码")
            code = wait_for_code(mailbox)
            if not code:
                raise RuntimeError("独立登录等待验证码超时")
            resp, reason = validate_otp(self.session, self.device_id, code)
            if resp is None or resp.status_code != 200:
                print("独立登录验证码校验失败响应:", resp.text if resp is not None else "None")
                data = _response_json(resp) if resp is not None else {}
                message = str((data.get("error") or {}).get("message") or data.get("message") or "").strip()
                raise RuntimeError(reason or f"独立登录验证码校验失败{': ' + message if message else ''}")
            otp_payload = _response_json(resp)
            continue_url = str(otp_payload.get("continue_url") or continue_url).strip()
            step(index, "独立登录验证码校验完成")
        if not continue_url:
            continue_url = f"{auth_base}/sign-in-with-chatgpt/codex/consent"
        tokens = exchange_platform_tokens(self.session, self.device_id, code_verifier, continue_url)
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        step(index, f"邮箱创建完成: {email}")
        password = _random_password()
        first_name, last_name = _random_name()
        self._platform_authorize(email, index)
        self._register_user(email, password, index)
        self._send_otp(index)
        step(index, "开始等待注册验证码")
        code = wait_for_code(mailbox)
        if not code:
            raise RuntimeError("等待注册验证码超时")
        step(index, f"收到注册验证码: {code}")
        self._validate_otp(code, index)
        self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
        tokens = self._login_and_exchange_tokens(email, password, mailbox, index)
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int) -> dict:
    start = time.time()
    registrar = PlatformRegistrar(config["proxy"])
    try:
        step(index, "任务启动")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_accounts([access_token])
        account_service.refresh_accounts([access_token])
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            if stats["success"] > 0:
                avg = (time.time() - stats["start_time"]) / stats["success"]
                log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
            else:
                log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
    start = time.time()
    registrar = PlatformRegistrar(config["proxy"])
    try:
        step(index, "任务启动")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_accounts([access_token])
        account_service.refresh_accounts([access_token])
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()


def start_register(total: int | None = None, threads: int | None = None) -> None:
    """
    启动批量注册任务
    
    Args:
        total: 要注册的账号总数
        threads: 并发线程数
    """
    if total is not None:
        config["total"] = total
    if threads is not None:
        config["threads"] = threads
    
    stats["start_time"] = time.time()
    stats["done"] = 0
    stats["success"] = 0
    stats["fail"] = 0
    
    log(f"开始批量注册，总数: {config['total']}, 线程数: {config['threads']}")
    
    with ThreadPoolExecutor(max_workers=config["threads"]) as executor:
        futures_list = []
        for i in range(1, config["total"] + 1):
            future = executor.submit(worker, i)
            futures_list.append(future)
            time.sleep(0.5)  # 避免同时启动过多任务
        
        # 等待所有任务完成
        for future in as_completed(futures_list):
            result = future.result()
            if result["ok"]:
                log(f"任务{result['index']} 完成", "green")
            else:
                log(f"任务{result['index']} 失败: {result['error']}", "red")
    
    total_time = time.time() - stats["start_time"]
    log(f"批量注册完成，成功: {stats['success']}, 失败: {stats['fail']}, 总耗时: {total_time:.1f}s", "green")