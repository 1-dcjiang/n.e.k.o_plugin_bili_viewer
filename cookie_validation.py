"""Cookie 验真编排：有限重试、正负缓存与同 Cookie 单飞。

本模块不导入 bili，避免与 HTTP/Cookie 存储层形成循环依赖。真实 nav 请求由调用方
注入；这里只负责并发与结果语义，单元测试使用假的 validator，不代表已对接真实 B站。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Callable

VERIFIED = "verified"
REJECTED = "rejected"
UNAVAILABLE = "unavailable"

# 登录态明确无效与网络/风控暂时不可验真必须分开：后者绝不能把合法 Cookie 判死。
_PERMANENT_CODES = {"BAD_COOKIE", "AUTH_FAILED", "NEED_LOGIN"}
_TRANSIENT_CODES = {
    "NETWORK",
    "TIMEOUT",
    "WRITE_OUTCOME_UNKNOWN",
    "RISK_CONTROL",
    "HTTP_ERROR",
    "BAD_RESPONSE",
}


@dataclass(frozen=True)
class CookieValidationResult:
    status: str
    uid: int | None = None
    code: str = ""
    from_cache: bool = False


@dataclass
class _CacheEntry:
    result: CookieValidationResult
    expires_at: float


@dataclass
class _Flight:
    event: threading.Event
    result: CookieValidationResult | None = None


class CookieVerifier:
    """按 Cookie 摘要缓存验真结果，并合并同 Cookie 的并发网络请求。

    - 成功默认缓存 10 分钟；
    - 明确无效默认缓存 5 分钟；
    - 网络、超时、-352 等风控暂时失败只负缓存 30 秒，返回 UNAVAILABLE；
    - 暂时失败总共最多尝试 2 次（一次短延迟重试），且锁只保护内存状态，
      validator 网络调用与重试 sleep 均不在锁内。
    """

    def __init__(
        self,
        validator: Callable[[str, float], int | None],
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        positive_ttl: float = 600.0,
        rejected_ttl: float = 300.0,
        negative_ttl: float = 30.0,
        max_attempts: int = 2,
        retry_delay: float = 0.15,
        max_entries: int = 64,
    ) -> None:
        self._validator = validator
        self._clock = clock
        self._sleeper = sleeper
        self._positive_ttl = float(positive_ttl)
        self._rejected_ttl = float(rejected_ttl)
        self._negative_ttl = float(negative_ttl)
        self._max_attempts = max(1, int(max_attempts))
        self._retry_delay = max(0.0, float(retry_delay))
        self._max_entries = max(4, int(max_entries))
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}
        self._flights: dict[str, _Flight] = {}

    @staticmethod
    def _key(cookie: str) -> str:
        # 不把完整 Cookie 作为长期内存字典键，降低凭证意外暴露面。
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()

    @staticmethod
    def _code(exc: BaseException) -> str:
        return str(getattr(exc, "code", "") or "VALIDATION_ERROR")

    def _run(self, cookie: str, timeout: float) -> CookieValidationResult:
        last_code = "VALIDATION_ERROR"
        for attempt in range(self._max_attempts):
            try:
                uid = int(self._validator(cookie, timeout) or 0)
                if uid > 0:
                    return CookieValidationResult(VERIFIED, uid=uid)
                # nav 已明确返回未登录，不需要网络重试。
                return CookieValidationResult(REJECTED, code="BAD_COOKIE")
            except Exception as exc:  # validator 的异常边界统一在此归一
                code = self._code(exc)
                last_code = code
                if code in _PERMANENT_CODES:
                    return CookieValidationResult(REJECTED, code="BAD_COOKIE")
                if code not in _TRANSIENT_CODES:
                    # 未预期异常按暂时不可验真处理，而不是误判 Cookie 永久失效。
                    last_code = code or "VALIDATION_ERROR"
                if attempt + 1 < self._max_attempts:
                    self._sleeper(self._retry_delay)
        return CookieValidationResult(UNAVAILABLE, code=last_code)

    def verify(self, cookie: str, timeout: float = 6.0) -> CookieValidationResult:
        key = self._key(str(cookie or ""))
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                if cached.expires_at > now:
                    old = cached.result
                    return CookieValidationResult(old.status, old.uid, old.code, from_cache=True)
                self._cache.pop(key, None)
            flight = self._flights.get(key)
            owner = flight is None
            if owner:
                flight = _Flight(threading.Event())
                self._flights[key] = flight

        if not owner:
            # 不在全局账号锁内等待；这里只等同 Cookie 的单飞结果。
            flight.event.wait(timeout=max(1.0, timeout * self._max_attempts + 5.0))
            if flight.result is not None:
                return flight.result
            return CookieValidationResult(UNAVAILABLE, code="VERIFY_IN_PROGRESS")

        result = self._run(str(cookie or ""), timeout)
        now = self._clock()
        ttl = (
            self._positive_ttl
            if result.status == VERIFIED
            else self._rejected_ttl
            if result.status == REJECTED
            else self._negative_ttl
        )
        flight.result = result
        with self._lock:
            self._cache[key] = _CacheEntry(result, now + ttl)
            if len(self._cache) > self._max_entries:
                for old_key in sorted(self._cache, key=lambda k: self._cache[k].expires_at)[:-self._max_entries]:
                    self._cache.pop(old_key, None)
            self._flights.pop(key, None)
            flight.event.set()
        return result
