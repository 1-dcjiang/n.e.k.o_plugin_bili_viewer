"""bili_viewer 自动绑定：读取本机 Chrome/Edge 已登录的 B站 Cookie。

- 仅本地读取（浏览器 Cookies 数据库），无网络传输、不写日志、不上传；
- 解密走 Windows 系统 API（Crypt32 DPAPI + BCrypt AES-256-GCM），零第三方依赖；
- 浏览器运行中 Cookies 文件被浏览器独占锁定时返回 "locked"，提示用户关闭浏览器后重试；
- 浏览器未登录 B站时返回 "not_logged_in"（只有游客 Cookie，无 SESSDATA 则无登录态）。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
from ctypes import wintypes

__all__ = ["detect_bili_cookie", "AUTO_COOKIE_SOURCES", "STATUS_TEXT"]

# 需要保留的 Cookie 名称（含登录态与风控所需）
_KEEP_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "buvid3",
    "buvid4",
    "b_nut",
    "CURRENT_FNVAL",
    "bili_ticket",
    "bili_ticket_expires",
    "sid",
    "fingerprint",
    "fingerprintp",
    "buvid_fp",
)

_STATUS_BOUND = "bound"           # 自动绑定成功
_STATUS_LOCKED = "locked"         # 浏览器运行中，文件被独占锁
_STATUS_NOT_LOGGED_IN = "not_logged_in"  # 浏览器可读但无登录态
_STATUS_NO_BROWSER = "no_browser" # 未找到浏览器数据目录
_STATUS_FAIL = "fail"             # 其他失败（解密异常等）

STATUS_TEXT = {
    _STATUS_BOUND: "已自动绑定 B站账号（自动读取浏览器 Cookie）",
    _STATUS_LOCKED: "浏览器正在运行，Cookie 被浏览器锁定；请先关闭 Chrome/Edge，然后重启客户端或再确认一次",
    _STATUS_NOT_LOGGED_IN: "浏览器未登录 B站（没有登录态 Cookie），请先在浏览器登录一次 bilibili.com",
    _STATUS_NO_BROWSER: "未在本机找到 Chrome/Edge 浏览器数据",
    _STATUS_FAIL: "自动读取浏览器 Cookie 失败（解密异常）",
}

# (浏览器名, User Data 根目录相对 LOCALAPPDATA 的路径)
AUTO_COOKIE_SOURCES: tuple[tuple[str, str], ...] = (
    ("Chrome", os.path.join("Google", "Chrome", "User Data")),
    ("Edge", os.path.join("Microsoft", "Edge", "User Data")),
)


# ══════════ DPAPI（Crypt32） ══════════


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_unprotect(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty blob")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("DPAPI CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ══════════ AES-256-GCM（BCrypt CNG） ══════════


class _AUTH_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("cbFlags", wintypes.ULONG),
    ]


_BCRYPT_AES = "AES"
_BCRYPT_CHAINING_MODE = "ChainingMode"
_BCRYPT_CHAIN_MODE_GCM = "ChainingModeGCM"
_BCRYPT_AUTH_MODE_INFO_VERSION = 1
_BCRYPT_HANDLE = ctypes.c_void_p
_BCRYPT_PHANDLE = ctypes.POINTER(_BCRYPT_HANDLE)


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    bcrypt = ctypes.windll.bcrypt
    _LONG = ctypes.c_long
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [_BCRYPT_PHANDLE, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD]
    bcrypt.BCryptOpenAlgorithmProvider.restype = _LONG
    bcrypt.BCryptSetProperty.argtypes = [_BCRYPT_HANDLE, ctypes.c_wchar_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    bcrypt.BCryptSetProperty.restype = _LONG
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        _BCRYPT_HANDLE, _BCRYPT_PHANDLE, ctypes.c_void_p, wintypes.ULONG,
        ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
    ]
    bcrypt.BCryptGenerateSymmetricKey.restype = _LONG
    bcrypt.BCryptDecrypt.argtypes = [
        _BCRYPT_HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ]
    bcrypt.BCryptDecrypt.restype = _LONG
    bcrypt.BCryptDestroyKey.argtypes = [_BCRYPT_HANDLE]
    bcrypt.BCryptDestroyKey.restype = _LONG
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [_BCRYPT_HANDLE, wintypes.DWORD]
    bcrypt.BCryptCloseAlgorithmProvider.restype = _LONG

    h_alg = _BCRYPT_HANDLE()
    h_key = _BCRYPT_HANDLE()
    try:
        if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), _BCRYPT_AES, None, 0) != 0:
            raise OSError("BCryptOpenAlgorithmProvider failed")
        mode = ctypes.create_unicode_buffer(_BCRYPT_CHAIN_MODE_GCM)
        if bcrypt.BCryptSetProperty(
            h_alg, _BCRYPT_CHAINING_MODE, mode, (len(_BCRYPT_CHAIN_MODE_GCM) + 1) * 2, 0
        ) != 0:
            raise OSError("BCryptSetProperty failed")
        key_buf = ctypes.create_string_buffer(key, len(key))
        if bcrypt.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0, key_buf, len(key), 0
        ) != 0:
            raise OSError("BCryptGenerateSymmetricKey failed")
        nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
        tag_buf = ctypes.create_string_buffer(tag, len(tag))
        auth = _AUTH_MODE_INFO()
        auth.cbSize = ctypes.sizeof(_AUTH_MODE_INFO)
        auth.dwInfoVersion = _BCRYPT_AUTH_MODE_INFO_VERSION
        auth.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
        auth.cbNonce = len(nonce)
        auth.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
        auth.cbTag = len(tag)
        out_len = wintypes.DWORD()
        if bcrypt.BCryptDecrypt(
            h_key, ciphertext, len(ciphertext),
            ctypes.byref(auth), None, 0,
            None, 0, ctypes.byref(out_len), 0,
        ) != 0:
            raise OSError("BCryptDecrypt(size) failed")
        out_buf = ctypes.create_string_buffer(out_len.value)
        if bcrypt.BCryptDecrypt(
            h_key, ciphertext, len(ciphertext),
            ctypes.byref(auth), None, 0,
            out_buf, out_len.value, ctypes.byref(out_len), 0,
        ) != 0:
            raise OSError("BCryptDecrypt failed")
        return out_buf.raw[: out_len.value]
    finally:
        if h_key:
            bcrypt.BCryptDestroyKey(h_key)
        if h_alg:
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


# ══════════ Cookie 解密与组装 ══════════


def _decrypt_cookie_value(encrypted_value: bytes, aes_key: bytes) -> str:
    if encrypted_value[:3] == b"v10" and len(encrypted_value) > 15 + 16:
        nonce = encrypted_value[3:15]
        tag = encrypted_value[-16:]
        ct = encrypted_value[15:-16]
        plain = _aes_gcm_decrypt(aes_key, nonce, ct, tag)
        try:
            return plain.decode("utf-8", errors="replace")
        except Exception:
            return plain.decode("latin-1", errors="replace")
    return ""


def build_cookie_string(items: list[tuple[str, str]]) -> str:
    """把 (name, value) 列表组装成 'a=1; b=2' 形式的 Cookie 字符串。"""
    parts = []
    seen: set[str] = set()
    for name, value in items:
        if not name or name in seen or not value:
            continue
        if any(ch in value for ch in ';",'):
            continue  # 值含非法字符则丢弃，避免破坏 Cookie 头
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _load_aes_key(local_state_path: str) -> bytes | None:
    try:
        with open(local_state_path, encoding="utf-8") as f:
            ls = json.load(f)
        raw = base64.b64decode(ls["os_crypt"]["encrypted_key"])
        if raw[:5] != b"DPAPI":
            return None
        return _dpapi_unprotect(raw[5:])
    except Exception:
        return None


def _open_cookies_db(cookies_path: str) -> sqlite3.Connection | None:
    uri = "file:///{}?mode=ro&immutable=1".format(cookies_path.replace("\\", "/"))
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1)
        con.execute("SELECT 1").fetchone()
        return con
    except Exception:
        return None


def _scan_profile(profile_dir: str, aes_key: bytes) -> tuple[str, list[tuple[str, str]]]:
    """扫描一个浏览器 profile：返回 (status, 该 profile 的 cookie 项)。"""
    cookies_db = os.path.join(profile_dir, "Network", "Cookies")
    if not os.path.exists(cookies_db):
        return _STATUS_NO_BROWSER, []
    con = _open_cookies_db(cookies_db)
    if con is None:
        return _STATUS_LOCKED, []
    try:
        rows = con.execute(
            "SELECT name, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%bilibili.com%' AND encrypted_value IS NOT NULL AND encrypted_value != ''"
        ).fetchall()
    except Exception:
        return _STATUS_FAIL, []
    finally:
        con.close()
    items: list[tuple[str, str]] = []
    for name, enc in rows:
        if name in _KEEP_NAMES:
            value = _decrypt_cookie_value(enc, aes_key)
            if value:
                items.append((name, value))
    if any(name == "SESSDATA" for name, _ in items):
        return _STATUS_BOUND, items
    if items:
        return _STATUS_NOT_LOGGED_IN, items  # 有游客 cookie，但无登录态
    return _STATUS_NOT_LOGGED_IN, []


def _iter_profiles(user_data_root: str):
    """产出 (profile 目录, cookies 路径) 候选：Default + Profile*。"""
    default = os.path.join(user_data_root, "Default")
    if os.path.isdir(default):
        yield default
    if os.path.isdir(user_data_root):
        for entry in sorted(os.listdir(user_data_root)):
            if entry.startswith("Profile"):
                yield os.path.join(user_data_root, entry)


def detect_bili_cookie() -> tuple[str, str, str]:
    """自动检测本机 Chrome/Edge 的 B站登录 Cookie。

    返回 (status, cookie_string, detail)：
      - status: bound / locked / not_logged_in / no_browser / fail
      - cookie_string: 仅 bound 时非空
      - detail: 人类可读说明
    """
    last_locked = False
    found_any_browser = False
    for browser_name, rel in AUTO_COOKIE_SOURCES:
        root = os.path.join(os.environ.get("LOCALAPPDATA", ""), rel)
        if not os.path.isdir(root):
            continue
        found_any_browser = True
        local_state = os.path.join(root, "Local State")
        aes_key = _load_aes_key(local_state)
        if aes_key is None:
            continue
        for profile_dir in _iter_profiles(root):
            status, items = _scan_profile(profile_dir, aes_key)
            if status == _STATUS_LOCKED:
                last_locked = True
                continue
            if status == _STATUS_BOUND:
                cookie = build_cookie_string(items)
                return _STATUS_BOUND, cookie, f"已从 {browser_name} 自动读取 B站登录态"
            if status == _STATUS_FAIL:
                continue
        if last_locked:
            break  # 该浏览器被锁定，后续浏览器大概率同样被锁，直接返回锁定状态
    if not found_any_browser:
        return _STATUS_NO_BROWSER, "", STATUS_TEXT[_STATUS_NO_BROWSER]
    if last_locked:
        return _STATUS_LOCKED, "", STATUS_TEXT[_STATUS_LOCKED]
    return _STATUS_NOT_LOGGED_IN, "", STATUS_TEXT[_STATUS_NOT_LOGGED_IN]
