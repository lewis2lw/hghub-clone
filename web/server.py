#!/usr/bin/env python3
"""HGhub clone web server (8081 端口，跑在学长香港服务器上)。

纯 Python 3 标准库 + 系统自带的 openssl 命令行，无第三方依赖。

它只做三件事：
  1. 提供复刻版网页（web/index.html，首页竖屏刷剧、分类/搜索/榜单/
     帖子/详情/登录、播放器）。
  2. 同源反代：浏览器只跟本站说话，本站再去调 8080 的播放代理和
     上游 huangguoai.com —— 这样前端不用直连被挡的 CDN 域名，
     也不用处理跨域。
  3. 封面图代理 /proxy/img：把加密的封面解密成正常 JPEG 再返回
     （见下面的“封面图为什么是加密的”）。

封面图为什么是加密的（2026-11-10 实测）
----------------------------------------
上游图床 pic.drleao.cn 下发的封面不是 JPEG，而是一串 AES 加密字节：
熵 7.999 bit/字节，全文件找不到任何 JPEG/PNG/WebP 文件头，Content-Type
还是 binary/octet-stream。浏览器直接拿它当 <img src> 永远显示不出来，
这就是网页端封面丢失的根因（跟 Referer、防盗链、CDN 封禁都没有关系，
curl 不带 Referer 也能拉到同样的 200，只是内容是密文）。

上游自己的解密协议是公开的（/static/web/js/plugins/crypto-image-protocol.js）：
  AES-128-CBC，无填充（NoPadding）
  decryptKey = "f5d965df75336270"（16 个 ASCII 字符 = 16 字节密钥）
  decryptIV  = "97b60394abc2fbe1"（16 个 ASCII 字符 = 16 字节 IV）
已用 openssl 本地验证：同样参数解密后得到合法 JPEG（FFD8FFE0 开头）。

本服务在服务端解密：/proxy/img 先拉取封面字节，再用 openssl 解密，
返回 image/jpeg。前端 index.html 里所有封面都走 /proxy/img?url=...，
失败时 onerror 换回直连地址再试一次（部分旧图可能是明文）。

路由
----
  GET  /                        -> web/dist/index.html（方案 B：HGhubX 前端构建产物；
       dist 缺失时回落 web/index.html 旧版单文件页）
  GET  /legacy                  -> web/index.html（旧版单文件前端，回滚对照用）
  GET  /assets/<file>           -> web/dist/assets/<file>（构建产物 JS/CSS）
  GET  /hls.min.js              -> web/hls.min.js（本地 hls.js，不再依赖 CDN）
  GET  /health                  -> {"status": "ok"}
  POST /api/play                -> 反代到 8080 播放代理 POST /api/play
  GET  /proxy/m3u8?url=...      -> 反代到 8080 /proxy/m3u8
  GET  /proxy/key?url=...       -> 反代到 8080 /proxy/key
  GET  /proxy/ts?url=...        -> 反代到 8080 /proxy/ts
  GET  /img?u=...                -> 封面：拉取 -> AES 解密 -> 校验魔数 -> 返回图片
       （HGhubX /img 移植；失败 502 text/plain，绝不 200 空内容）
  GET  /hls?u=...                -> HLS 清单：拉取 -> 改写 URI 为本地 /seg?u= -> 返回
       （HGhubX /hls 移植；非 m3u8 / 非 UTF-8 返回 502）
  GET  /seg?u=...                -> 分片/密钥/嵌套清单：支持单段 Range（206/416）
       （HGhubX /seg 移植；嵌套清单继续改写）
  POST /api/tauri                -> HGhubX 全部 Tauri 命令分发（JSON {cmd, args}）
       （命令映射见 ~/workspace/hghubx-work/mapping.md；返回 {ok, data} /
       {ok: false, error}，错误字符串尽量沿用 Rust 原文）
  GET  /api/up/<path>?<query>   -> 反代到 https://huangguoai.com/api/<path>
       （查询串原样转发：?page= / cate_id= / q= 都透传，不截断；
        1.2.0.3 分页就靠它：/api/videos 全量 3361 部/169 页，每页 20 条）
  GET  /api/edge/*              -> 反代到 https://huangguoai.com/api/edge/*
  GET/POST /api/*               -> 反代到 https://huangguoai.com/api/*（1.2.0.6
       新增通用路由：/api/videos、/api/search、/api/ranks、/api/posts、
       /api/site/*、/api/videos/:id、/api/videos/:id/comments 直接调也不 404；
       /api/play、/api/prefip 在前面优先匹配，不走这条）

环境变量：HOST（默认 0.0.0.0）、PORT（默认 8081）、
PROXY_BASE（默认 http://127.0.0.1:8080）、UPSTREAM（默认 https://huangguoai.com）。
"""

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8081"))
PROXY_BASE = os.environ.get("PROXY_BASE", "http://127.0.0.1:8080").rstrip("/")
UPSTREAM = os.environ.get("UPSTREAM", "https://huangguoai.com").rstrip("/")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
# 方案 B：HGhubX 前端构建产物（web/dist/，由 hghubx-src 的 pnpm build 生成后拷入）。
# / 优先 serving dist/index.html；dist 缺失时回落到旧版单文件 index.html。
DIST_DIR = os.path.join(BASE_DIR, "dist")
DIST_INDEX_PATH = os.path.join(DIST_DIR, "index.html")
DIST_ASSETS_DIR = os.path.join(DIST_DIR, "assets")

USER_AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"

# 封面解密参数：上游公开的前端协议（crypto-image-protocol.js），
# 与后台配置无关，写死在这里是安全的 —— 它本来就是公开给浏览器的。
IMG_KEY = b"f5d965df75336270"
IMG_IV = b"97b60394abc2fbe1"

# /proxy/img 只放行这几个图床域名，防止被当成任意代理滥用。
IMG_HOSTS = {
    "pic.drleao.cn", "new.drleao.cn", "image.drleao.cn",
    "new2.drleao.cn", "pic.aluxvl.cn", "huangguoai.com",
    "pic.qnlycc.cn",  # 上游图床已迁移到 qnlycc.cn（2026-11-23 联调实测）
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("hghub-web")


def _fetch(url, data=None, headers=None, timeout=25):
    req = urllib.request.Request(url, data=data,
                                 method="POST" if data is not None else "GET")
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get_content_type()
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get_content_type() if e.headers else "text/plain"


def _sniff_image(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _aes_decrypt_cbc_nopad(data: bytes) -> bytes | None:
    """用系统 openssl 解密 AES-128-CBC（无填充）。失败返回 None。"""
    if len(data) == 0 or len(data) % 16 != 0:
        return None
    try:
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-cbc", "-nopad",
             "-K", IMG_KEY.hex(), "-iv", IMG_IV.hex()],
            input=data, capture_output=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


class _ImgCache:
    """解密后的封面小缓存：同一张图只解密一次（内存 LRU，最多 200 张）。"""

    def __init__(self, limit=200):
        self.limit = limit
        self.store = OrderedDict()

    def get(self, key):
        hit = self.store.get(key)
        if hit is not None:
            self.store.move_to_end(key)
        return hit

    def put(self, key, value):
        self.store[key] = value
        self.store.move_to_end(key)
        while len(self.store) > self.limit:
            self.store.popitem(last=False)


IMG_CACHE = _ImgCache()

# 优选 IP 缓存（1.2.0.5）：cf.090227.xyz 是别人测好的 CF 优选 IP 列表，
# 纯文本，"IP#CF 电信优选" 逗号或换行分隔；/ct 电信、/cu 联通、/cmcc 移动。
# 浏览器直接跨域拉会被 CORS 挡住，所以由本站服务端拉取解析再返回 JSON。
PREFIP_CACHE = {"at": 0, "data": {}}
PREFIP_TTL = 600


def _prefip_list(isp):
    """拉一个运营商的优选 IP 列表，返回 [ip, ...]（失败返回 []）。"""
    import re as _re
    url = "https://cf.090227.xyz/" + isp
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        log.warning("prefip fetch %s failed: %s", isp, e)
        return []
    return _re.findall(r"(\d+\.\d+\.\d+\.\d+)\s*#", text)


def _prefip_all():
    import time as _time
    now = _time.time()
    if now - PREFIP_CACHE["at"] < PREFIP_TTL and PREFIP_CACHE["data"]:
        return PREFIP_CACHE["data"]
    data = {k: _prefip_list(k) for k in ("ct", "cu", "cmcc")}
    PREFIP_CACHE["at"] = now
    PREFIP_CACHE["data"] = data
    return data


# =====================================================================
# /api/tauri 与 /img /hls /seg（方案 B：HGhubX 前端整体替换）
#
# 事实来源：~/workspace/hghubx-work/mapping.md（唯一事实来源）与
# ~/workspace/hghubx-src/src-tauri/src/{api,media,lines,net}/*.rs。
# Python 端不照搬 Rust 的 POST /api/edge/request 信封，直接调上游
# {base}/api/...，但返回结构与错误字符串尽量与 Rust 的 Result<T, String>
# 一致（见 mapping.md §0.1、§1.8）。
#
# 已知偏差（相对 Rust，见 mapping.md §7）：
#   - SSR 优先的 5 条命令（api_category / api_original_videos /
#     api_rank 的 hot / api_post_category 的 remen-chigua）一期只走
#     JSON 回退链，不抓 SSR HTML；api_search_hot 没有 JSON 接口，
#     必须解析上游 HTML（已实现，见 _fetch_search_hot）。
#   - app_boot.strict_security 默认 false（Web 端无 Tauri 严格模式
#     强制执行；security_set 可改并落盘），Rust 默认 true。
#   - media_http_origin 返回请求 Host 对应的 origin（任务要求），
#     而不是 Rust 的“仅 iOS，否则 Err”。
#   - check_update 固定返回 has_update=false，不调 GitHub。
#   - line_mirrors 的发布页 JS 解析是近似实现（只提 https origin
#     字符串，不做 Rust publish_parser 的严格词法解析）。
# =====================================================================

class TauriError(Exception):
    """对应 Rust 命令的 Err(String)；文案尽量沿用 Rust 原文。"""


DEFAULT_BASE = "https://huangguoai.com"
FIXED_LINE = {"name": "固定线路 1", "url": DEFAULT_BASE}
PUBLISH_PAGES = ["https://huangguoai.pages.dev/", "https://huang-guo.pages.dev/"]
MIRROR_MAX = 60

STATE_LOCK = threading.Lock()
LINE_GENERATION = {"n": 0}
PLAYBACK_LOG = {"enabled": False, "initialized": False}

SESSION_FILE = os.environ.get("HGHUB_SESSION_FILE") or os.path.join(BASE_DIR, "session.json")
LINE_FILE = os.path.join(BASE_DIR, "line.json")
PROXY_FILE = os.path.join(BASE_DIR, "proxy.json")
SECURITY_FILE = os.path.join(BASE_DIR, "security.json")
TRUSTED_MIRRORS_FILE = os.path.join(BASE_DIR, "trusted_mirrors.json")
DEBUG_LOG_FILE = os.path.join(BASE_DIR, "debug.log")
PLAYBACK_LOG_FILE = os.path.join(BASE_DIR, "playback.log")
PLAYBACK_PREV_LOG_FILE = os.path.join(BASE_DIR, "playback.previous.log")

def _media_url_allowed(url):
    """媒体拉取的地址检查（对齐 Rust fetch_bytes：只做 scheme/字面 IP 检查，
    不设固定域名白名单——上游 CDN 域名会轮换，如 pic.qnlycc.cn /
    yd-lphu-hls.tjyyjk.cn；字面内网/回环 IP 拒绝；http 拒绝防降级。
    不做 DNS 解析检查：本机出口代理下公网域名可能解析到 CGNAT 地址，
    按解析结果拦截会误杀正常 CDN。"""
    if not url.startswith("https://"):
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    except ValueError:
        pass
    return True


# ------------------------- 状态文件（原子写 + 600） -------------------------

def _load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (FileNotFoundError, ValueError, OSError):
        return dict(default)


def _save_json_file(path, obj, private=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def _load_session():
    with STATE_LOCK:
        data = _load_json_file(SESSION_FILE, {})
    if not isinstance(data, dict) or not data.get("token"):
        return None
    return data


def _save_session(sess):
    with STATE_LOCK:
        sess["revision"] = int(sess.get("revision") or 0) + 1
        _save_json_file(SESSION_FILE, sess, private=True)
    return sess


def _clear_session():
    with STATE_LOCK:
        try:
            os.remove(SESSION_FILE)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _session_revision():
    sess = _load_session()
    return int((sess or {}).get("revision") or 0)


def _load_line():
    data = _load_json_file(LINE_FILE, {})
    base = data.get("base") or DEFAULT_BASE
    return {"base": base, "custom": bool(data.get("custom"))}


def _save_line(base, custom):
    _save_json_file(LINE_FILE, {"v": 2, "base": base, "custom": bool(custom)})


def _get_base():
    return _load_line()["base"]


def _load_trusted_mirrors():
    try:
        with open(TRUSTED_MIRRORS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, str)] if isinstance(data, list) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def _save_trusted_mirrors(items):
    _save_json_file(TRUSTED_MIRRORS_FILE, list(items)[:256])


def _is_fixed(url):
    return url == DEFAULT_BASE


def _is_trusted_mirror(url):
    return url in _load_trusted_mirrors()


def _trusted_auth_origin(origin):
    """token 可发给的 origin：固定主站、已确认镜像、已保存的自定义线路自身。"""
    if _is_fixed(origin) or _is_trusted_mirror(origin):
        return True
    line = _load_line()
    return bool(line["custom"]) and line["base"] == origin


def _load_security():
    return _load_json_file(SECURITY_FILE, {"strict": False})


def _load_proxy():
    data = _load_json_file(PROXY_FILE, {})
    mode = data.get("mode") or "system"
    if mode not in ("system", "custom", "off"):
        mode = "system"
    return {"mode": mode, "url": data.get("url") or ""}


def _save_proxy(mode, url):
    _save_json_file(PROXY_FILE, {"mode": mode, "url": url})


# ------------------------- 上游请求（直接调，不走 edge 信封） -------------------------

def _proxy_opener():
    st = _load_proxy()
    if st["mode"] == "off":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if st["mode"] == "custom" and st["url"]:
        return urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": st["url"], "https": st["url"]}))
    return urllib.request.build_opener()  # system：读环境变量代理


def _raw_request(method, url, data=None, headers=None, timeout=25,
                 max_bytes=8 * 1024 * 1024):
    """返回 (status, final_url, body_bytes, content_type)。
    HTTP 错误状态返回体而不抛；网络错误抛 TauriError。"""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _proxy_opener().open(req, timeout=timeout) as resp:
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise TauriError("响应超过大小限制")
            return resp.status, resp.geturl(), body, resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        body = e.read(max_bytes + 1)
        ctype = e.headers.get("Content-Type") if e.headers else None
        return e.code, url, body, ctype
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TauriError(f"请求失败：{e}")


def _edge_call(method, path, body=None, token=None, timeout=25):
    """直接调 {base}<path>，按 edge.rs 语义解包信封并归一化错误。"""
    base = _get_base()
    url = base.rstrip("/") + path
    if method == "POST":
        data = json.dumps(body if body is not None else {}).encode("utf-8")
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Referer": base.rstrip("/") + "/"}
    if token:
        headers["Authorization"] = "Bearer " + token
    attempts = 3 if method == "GET" else 1  # 写操作绝不重试（edge.rs）
    delay = 0.35
    last_exc = None
    for attempt in range(attempts):
        try:
            status, _final, raw, _ct = _raw_request(method, url, data, headers, timeout)
        except TauriError as e:
            last_exc = e
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay = 0.7
                continue
            raise
        if status in (401, 403):
            raise TauriError("401")  # 文案必须精确为 401（前端靠它触发静默重登）
        if status in (408, 425, 429) or 500 <= status <= 599:
            if method == "GET" and attempt + 1 < attempts:
                time.sleep(delay)
                delay = 0.7
                continue
            raise TauriError(f"HTTP {status}: {raw[:160].decode('utf-8', 'ignore')}")
        if status < 200 or status >= 300:
            raise TauriError(f"HTTP {status}: {raw[:160].decode('utf-8', 'ignore')}")
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise TauriError("接口返回失败")
        if isinstance(payload, dict) and "status" in payload:
            if payload.get("status") == 1:
                return payload.get("data")
            raise TauriError(str(payload.get("msg") or "接口返回失败"))
        return payload
    raise last_exc or TauriError("请求失败")


def _token_for_read():
    """只读命令附带 token 的条件：有会话、绑定当前线路、严格模式下 origin 可信。"""
    sess = _load_session()
    if not sess or sess.get("origin") != _get_base():
        return None
    if _load_security().get("strict", False) and not _trusted_auth_origin(_get_base()):
        return None
    return sess.get("token")


# ------------------------- 参数归一化（camelCase / snake_case 双兼容） -------------------------

def _arg(args, camel, snake, default=None):
    if not isinstance(args, dict):
        return default
    if camel in args and args[camel] is not None:
        return args[camel]
    if snake in args and args[snake] is not None:
        return args[snake]
    return default


def _int_arg(args, camel, snake, default):
    try:
        return int(_arg(args, camel, snake, default))
    except (TypeError, ValueError):
        return default


def _clamp_page(page):
    try:
        return max(1, int(page))
    except (TypeError, ValueError):
        return 1


def _clamp_size(size, default=20):
    try:
        return min(100, max(1, int(size)))
    except (TypeError, ValueError):
        return default


# ------------------------- 账号会话 -------------------------

def _account_session(args, require_identity=False):
    """对应 Rust account_session：无会话 -> '请先登录'；identity 不符 ->
    '账号或线路已变化，请重试'。"""
    sess = _load_session()
    if not sess or not sess.get("token") or sess.get("origin") != _get_base():
        raise TauriError("请先登录")
    uid = _arg(args, "expectedUserId", "expected_user_id")
    org = _arg(args, "expectedOrigin", "expected_origin")
    if require_identity and (uid is None or org is None):
        raise TauriError("账号或线路已变化，请重试")
    if uid is not None:
        try:
            if int(uid) != int((sess.get("user") or {}).get("id") or 0):
                raise TauriError("账号或线路已变化，请重试")
        except (TypeError, ValueError):
            raise TauriError("账号或线路已变化，请重试")
    if org is not None and org != _get_base():
        raise TauriError("账号或线路已变化，请重试")
    return sess


def _normalize_account(data, origin):
    """normalize_account：只留白名单字段，token 永不返回。"""
    if not isinstance(data, dict):
        raise TauriError("账号资料响应无效，请重新登录")
    user = data.get("user")
    if not isinstance(user, dict):
        raise TauriError("账号资料响应无效，请重新登录")
    try:
        uid = int(user.get("id") or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid <= 0:
        raise TauriError("账号资料响应无效，请重新登录")
    allowed = ("id", "username", "nickname", "avatar", "avatar_color",
               "sign", "desc", "user_type", "counts", "interactions")
    clean = {k: user[k] for k in allowed if k in user}
    snap = {"user": clean, "origin": origin}
    if isinstance(data.get("interactions"), dict):
        snap["interactions"] = data["interactions"]
    return snap


def _normalize_play_info(data):
    """normalize_play_info：streams.main/hevc 回填 video_url(_hevc)。"""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    streams = out.get("streams") or {}
    if isinstance(streams, dict):
        if streams.get("main") and not out.get("video_url"):
            out["video_url"] = streams["main"]
        if streams.get("hevc") and not out.get("video_url_hevc"):
            out["video_url_hevc"] = streams["hevc"]
    if not out.get("video_url") and out.get("video_url_hevc"):
        out["video_url"] = out["video_url_hevc"]
    return out


# ------------------------- 热搜 HTML 解析（无 JSON 接口，见 mapping §7 坑 5） -------------------------

class _HotParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found_section = False
        self.in_section = False
        self.cur = None
        self.field = None
        self.items = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class") or ""
        if tag == "section" and "hg-search-hot" in cls.split():
            self.found_section = True
            self.in_section = True
        elif tag == "a" and self.in_section and "hg-search-hot__item" in cls.split():
            href = attrs.get("href") or ""
            m = re.fullmatch(r"/video/(\d+)/", href)
            vid = int(m.group(1)) if m else None
            if vid and 0 < vid <= 9007199254740991:
                self.cur = {"video_id": vid, "title": "",
                            "plays": "", "fallback": attrs.get("data-search-hot") or ""}
            else:
                self.cur = None
        elif tag == "span" and self.cur is not None:
            if "hg-search-hot__name" in cls.split():
                self.field = "title"
            elif "hg-search-hot__plays" in cls.split():
                self.field = "plays"

    def handle_endtag(self, tag):
        if tag == "section" and self.in_section:
            self.in_section = False
        elif tag == "a" and self.cur is not None:
            title = " ".join((self.cur["title"] or self.cur["fallback"]).split())
            plays = " ".join(self.cur["plays"].split())
            vid = self.cur["video_id"]
            if title and plays and vid not in self.seen:
                self.seen.add(vid)
                self.items.append({"video_id": vid, "title": title, "plays": plays})
            self.cur = None
            self.field = None
        elif tag == "span":
            self.field = None

    def handle_data(self, data):
        if self.cur is not None and self.field:
            self.cur[self.field] += data


def _fetch_search_hot():
    base = _get_base()
    url = base.rstrip("/") + "/search/"
    try:
        status, _final, raw, _ct = _raw_request(
            "GET", url, None,
            {"Accept": "text/html,application/xhtml+xml", "Referer": base.rstrip("/") + "/"},
            timeout=12, max_bytes=2 * 1024 * 1024)
    except TauriError as e:
        raise TauriError(f"获取官网热搜失败：{e}")
    if status != 200:
        raise TauriError(f"获取官网热搜失败：官网搜索页请求失败（HTTP {status}）")
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TauriError("获取官网热搜失败：官网搜索页不是有效的 UTF-8 HTML")
    parser = _HotParser()
    parser.feed(html)
    if not parser.found_section:
        raise TauriError("获取官网热搜失败：官网搜索页没有找到热搜榜区域")
    if not parser.items:
        raise TauriError("获取官网热搜失败：官网热搜榜没有可用条目")
    return {"items": parser.items[:20]}


# ------------------------- 线路：校验 / 镜像 / 探测 -------------------------

def _normalize_base(url):
    """纯 HTTPS origin 校验：带路径/查询/认证/片段、非 HTTPS、内网地址 -> None。"""
    if not isinstance(url, str):
        return None
    try:
        u = urllib.parse.urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme != "https" or u.username or u.password or u.fragment or u.query:
        return None
    if u.path not in ("", "/"):
        return None
    host = (u.hostname or "").lower()
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return None
    except ValueError:
        pass  # 域名：字面 IP 检查通过（Rust 还会做 DNS 固定，此处不展开）
    return "https://" + host + (f":{u.port}" if u.port else "")


def _apply_base(base, custom):
    """切换线路：目标可信则会话 rebase（token 不变），否则清空会话。"""
    sess = _load_session()
    if sess and sess.get("token"):
        if _trusted_auth_origin(base) or _is_fixed(base):
            sess["origin"] = base
            _save_session(sess)
        else:
            _clear_session()
    _save_line(base, custom)


def _healthy_video_data(data):
    if not isinstance(data, dict):
        return False
    items = data.get("items")
    pg = data.get("pagination")
    if not isinstance(items, list) or not isinstance(pg, dict):
        return False
    for key in ("page", "size", "pages", "total"):
        if not isinstance(pg.get(key), int):
            return False
    if pg["page"] <= 0 or pg["size"] <= 0:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("id"), int) or item["id"] <= 0:
            return False
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return False
    return True


def _probe_hit(tier, origin):
    started = time.time()
    hit = {"tier": tier, "url": origin, "ok": False, "reachable": False,
           "ms": 0, "status": None}
    url = origin.rstrip("/") + "/api/videos?page=1&size=1"
    try:
        status, _final, raw, _ct = _raw_request(
            "GET", url, None, {"Accept": "application/json"}, timeout=5,
            max_bytes=1024 * 1024)
        hit["status"] = status
        if status == 200:
            hit["reachable"] = True
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("status") == 1:
                if _healthy_video_data(payload.get("data")):
                    hit["ok"] = True
                else:
                    hit["error"] = "接口未返回有效视频列表数据"
            else:
                hit["error"] = f"HTTP {status}"
        else:
            hit["error"] = f"HTTP {status}"
    except TauriError as e:
        msg = str(e)
        if "timed out" in msg or "Timeout" in msg:
            hit["error"] = "视频接口探测超时（5 秒）"
        else:
            hit["error"] = msg
    hit["ms"] = int((time.time() - started) * 1000)
    return hit


def _fetch_publish_page(page):
    """抓一个发布页：HTML -> 脚本地址 -> JS 文本 -> 提取 https origin 列表。"""
    status, _final, raw, _ct = _raw_request(
        "GET", page, None, {"Accept": "text/html,application/javascript"},
        timeout=8, max_bytes=512 * 1024)
    if status != 200:
        raise TauriError(f"HTTP {status}")
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TauriError("地址发布页或脚本不是有效 UTF-8")
    m = re.search(r"<script[^>]+src=[\"']([^\"']+\.js[^\"']*)[\"']", html, re.I)
    if not m:
        raise TauriError("地址发布页里没有找到发布脚本")
    script = urllib.parse.urljoin(page, m.group(1))
    status, _final, raw, _ct = _raw_request(
        "GET", script, None, {"Accept": "application/javascript"},
        timeout=8, max_bytes=512 * 1024)
    if status != 200:
        raise TauriError(f"HTTP {status}")
    try:
        js = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TauriError("地址发布页或脚本不是有效 UTF-8")
    items = []
    seen = set()
    for host in re.findall(r"https://([A-Za-z0-9.\-]+)", js):
        origin = "https://" + host.lower()
        if host.lower().endswith((".pages.dev",)) or host.lower() == "huangguoai.ai":
            continue
        if origin not in seen:
            seen.add(origin)
            items.append({"url": origin, "host": host.lower()})
    return {"source": page, "script": script, "items": items[:256]}


def _fetch_mirrors():
    """抓两个发布页合并；两方都失败则抛错；成功则记入可信镜像集合。"""
    pages, errors = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(_fetch_publish_page, p): p for p in PUBLISH_PAGES}
        for fut, page in futs.items():
            try:
                pages.append(fut.result())
            except TauriError as e:
                errors.append(f"{page}: {e}")
            except Exception as e:
                errors.append(f"{page}: {e}")
    items, seen = [], set()
    for index in range(MIRROR_MAX):
        for page in pages:
            if index < len(page["items"]):
                origin = page["items"][index]["url"]
                if origin not in seen and len(items) < MIRROR_MAX:
                    seen.add(origin)
                    items.append(page["items"][index])
    if not items:
        raise TauriError("；".join(errors) if errors else "地址发布页里没有可用的镜像地址")
    trusted = _load_trusted_mirrors()
    for item in items:
        if item["url"] not in trusted:
            trusted.append(item["url"])
    _save_trusted_mirrors(trusted)
    recommended, rec_seen = [], set()
    current = _normalize_base(_get_base())
    if current:
        host = urllib.parse.urlparse(current).hostname or ""
        rec_seen.add(current)
        recommended.append({"url": current, "host": host})
    for item in items:
        if len(recommended) >= 20:
            break
        if item["url"] not in rec_seen:
            rec_seen.add(item["url"])
            recommended.append(item)
    sources = ", ".join(p["source"] for p in pages)
    scripts = ", ".join(p["script"] for p in pages)
    return {"source": sources, "script": scripts, "items": items,
            "recommended_source": sources, "recommended": recommended,
            "errors": errors}


# ------------------------- 媒体：HLS 改写 / Range -------------------------

def _hls_enc(s):
    """percent-encode 全部非字母数字字符（Rust NON_ALPHANUMERIC）。"""
    out = []
    for b in s.encode("utf-8"):
        ch = chr(b)
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append(f"%{b:02X}")
    return "".join(out)


def _absolutize(base, target):
    if target.startswith(("http://", "https://")):
        return target
    return urllib.parse.urljoin(base, target)


def _hls_attributes(line):
    """解析 #TAG:KEY=VAL,...（引号内逗号不切分），返回 (tag, [attr...])。"""
    if ":" not in line:
        return None
    tag, rest = line.split(":", 1)
    attrs, quoted, start = [], False, 0
    for i, ch in enumerate(rest):
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            attrs.append(rest[start:i])
            start = i + 1
    attrs.append(rest[start:])
    return tag, attrs


def _map_uri_attributes(line, rewrite):
    parsed = _hls_attributes(line)
    if parsed is None:
        return line
    tag, attrs = parsed
    mapped = []
    for attr in attrs:
        if "=" in attr:
            key, value = attr.split("=", 1)
            if key.strip().upper().endswith("URI"):
                v = value.strip()
                if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                    mapped.append(f'{key.strip()}="{rewrite(v[1:-1])}"')
                    continue
        mapped.append(attr)
    return f"{tag}:{','.join(mapped)}"


def _rewrite_playlist(base_url, text, prefix="", direct=False):
    """按 media/hls.rs rewrite_playlist 逐行改写；prefix 为同源空前缀时
    生成 /seg?u=... 相对地址；direct=True 时直接返回上游绝对地址，
    播放器直连 CDN 取分片/密钥，不再经过本站中转（上游回 ACAO:*，
    跨域直取没问题；分片、清单、密钥三路直连实测 2026-11-23）。"""
    out = []
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.rstrip()
        if not line:
            out.append("")
            continue
        if line.startswith("#"):
            if line.startswith("#EXT-X-CONTENT-STEERING:"):
                continue
            if line.startswith("#EXT-X-DATERANGE:"):
                parsed = _hls_attributes(line)
                if parsed is not None:
                    tag, attrs = parsed
                    safe = [a for a in attrs
                            if "URI" not in a.split("=", 1)[0].strip().upper()
                            and a.split("=", 1)[0].strip().upper() != "X-ASSET-LIST"]
                    out.append(f"{tag}:{','.join(safe)}")
                continue
            if "URI" in line.upper() and '"' in line:
                if direct:
                    out.append(_map_uri_attributes(
                        line, lambda uri: _absolutize(base_url, uri)))
                else:
                    out.append(_map_uri_attributes(
                        line, lambda uri: f"{prefix}/seg?u={_hls_enc(_absolutize(base_url, uri))}"))
            else:
                out.append(line)
        else:
            absolute = _absolutize(base_url, line)
            if direct:
                out.append(absolute)
            else:
                out.append(f"{prefix}/seg?u={_hls_enc(absolute)}")
        # 每行以换行结尾（Rust 同款）
    return "\n".join(out) + ("\n" if out else "")


def _strip_pkcs7(buf):
    """仅当末 pad 字节全等于 pad（1..16）时才截断（image.rs strip_padding）。"""
    if not buf:
        return buf
    pad = buf[-1]
    if 0 < pad <= 16 and len(buf) >= pad and all(b == pad for b in buf[-pad:]):
        return buf[:-pad]
    return buf


PL_CACHE = OrderedDict()   # 清单缓存，最多 60 条
SEG_CACHE = OrderedDict()  # 分片/密钥缓存，最多 36 条，单条 <= 4 MiB


def _cache_get(cache, key, limit):
    hit = cache.get(key)
    if hit is not None:
        cache.move_to_end(key)
    return hit


def _cache_put(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


# ------------------------- /api/tauri 命令实现 -------------------------
# 返回值 = 解包后的上游 data（与 types.ts 一致）；失败抛 TauriError。
# P0 4 / P1 25 / P2 13 / P3 13 / P4 12 = 67 条（mapping.md §2）。

def _cmd_app_boot(args, ctx):
    base = _get_base()
    sess = _load_session()
    strict = bool(_load_security().get("strict", False))
    user, logged_in = None, False
    if sess and sess.get("token") and sess.get("user"):
        if sess.get("origin") == base:
            user, logged_in = sess["user"], True
        elif _trusted_auth_origin(sess.get("origin") or "") and _trusted_auth_origin(base):
            sess["origin"] = base  # 两端都可信才 rebase（session.rs）
            _save_session(sess)
            user, logged_in = sess["user"], True
    # 不编造 pending_migration_identity（mapping §7 坑 1）
    return {"user": user, "logged_in": logged_in, "base": base,
            "strict_security": strict, "cli_relaxed": False}


def _cmd_media_http_origin(args, ctx):
    # 任务要求：Web 端返回 location.origin（请求 Host 对应的 origin），
    # 而非 Rust 的“仅 iOS，否则 Err(原生媒体 HTTP 通道仅适用于 iOS)”。
    host = (ctx or {}).get("host") or ""
    if not host:
        return ""
    proto = (ctx or {}).get("proto") or "http"
    return f"{proto}://{host}"


def _cmd_security_set(args, ctx):
    strict = bool(_arg(args, "strict", "strict", False))
    _save_json_file(SECURITY_FILE, {"strict": strict})
    return strict


def _cmd_debug_log(args, ctx):
    msg = str(_arg(args, "msg", "msg", ""))
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{os.getpid()}] {msg}\n")
    except OSError:
        pass
    return None


# ---- P1 内容只读 ----

def _cmd_api_feed(args, ctx):
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    return _edge_call("GET", f"/api/videos?page={page}&size={size}",
                      token=_token_for_read())


def _cmd_api_topics(args, ctx):
    return _edge_call("GET", "/api/topics?size=100", token=_token_for_read())


def _cmd_api_topic(args, ctx):
    slug = urllib.parse.quote(str(_arg(args, "slug", "slug", "")), safe="")
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    return _edge_call("GET", f"/api/topics/{slug}?page={page}&size={size}",
                      token=_token_for_read())


def _cmd_api_tags(args, ctx):
    return _edge_call("GET", "/api/tags", token=_token_for_read())


def _cmd_api_tag_videos(args, ctx):
    tag = urllib.parse.quote(str(_arg(args, "tag", "tag", "")), safe="")
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    sort = _arg(args, "sort", "sort", "latest")
    sort = "hot" if sort == "hot" else "latest"
    return _edge_call("GET", f"/api/videos/tag/{tag}?page={page}&size={size}&sort={sort}",
                      token=_token_for_read())


def _cmd_api_post_tag(args, ctx):
    tag = urllib.parse.quote(str(_arg(args, "tag", "tag", "")), safe="")
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    return _edge_call("GET", f"/api/posts/tag/{tag}?page={page}&size={size}",
                      token=_token_for_read())


def _cmd_api_category(args, ctx):
    # 已知偏差：跳过 SSR，只走 JSON 回退链（mapping §7 坑 5）。
    cate = str(_arg(args, "cate", "cate", ""))
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    sort = _arg(args, "sort", "sort")
    suffix = f"&sort={urllib.parse.quote(str(sort), safe='')}" if sort else ""
    if cate == "newest":
        path = f"/api/videos?page={page}&size={size}&sort=new"
    elif cate == "recommend":
        path = f"/api/videos?page={page}&size={size}&sort=hot"
    else:
        path = (f"/api/videos/category/{urllib.parse.quote(cate, safe='')}"
                f"?page={page}&size={size}{suffix}")
    return _edge_call("GET", path, token=_token_for_read())


def _cmd_api_original_videos(args, ctx):
    cate = str(_arg(args, "cate", "cate", ""))
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    sort = _arg(args, "sort", "sort")
    suffix = f"&sort={urllib.parse.quote(str(sort), safe='')}" if sort else ""
    if cate == "all":
        path = f"/api/videos?is_original=1&page={page}&size={size}{suffix}"
    else:
        path = (f"/api/videos/category/{urllib.parse.quote(cate, safe='')}"
                f"?is_original=1&page={page}&size={size}{suffix}")
    return _edge_call("GET", path, token=_token_for_read())


def _cmd_api_rank(args, ctx):
    kind = str(_arg(args, "kind", "kind", "hot"))
    if kind in ("recommend", "potential"):
        return _edge_call("GET", f"/api/ranks/{kind}", token=_token_for_read())
    # hot：已知偏差，跳过 SSR 抓 /ranks/hot/，直接走 JSON 回退。
    return _edge_call("GET", "/api/ranks/hot", token=_token_for_read())


def _cmd_api_search(args, ctx):
    q = str(_arg(args, "q", "q", ""))
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    kind = _arg(args, "kind", "kind")
    # kind 省略 -> 不带 type，只搜短剧（mapping §7 坑 6）
    suffix = ""
    if kind == "post":
        suffix = "&type=post"
    elif kind == "video":
        suffix = "&type=video"
    return _edge_call(
        "GET", f"/api/search?q={urllib.parse.quote(q, safe='')}&page={page}&size={size}{suffix}",
        token=_token_for_read())


def _cmd_api_search_hot(args, ctx):
    return _fetch_search_hot()


def _cmd_api_posts(args, ctx):
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    sort = _arg(args, "sort", "sort")
    suffix = f"&sort={urllib.parse.quote(str(sort), safe='')}" if sort else ""
    return _edge_call("GET", f"/api/posts?page={page}&size={size}{suffix}",
                      token=_token_for_read())


def _cmd_api_post(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    return _edge_call("GET", f"/api/posts/{vid}", token=_token_for_read())


def _cmd_api_post_playback(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    # POST 空对象 {}（mapping §7 坑 4）；上游 status=0 时按 Err 抛给前端。
    return _edge_call("POST", f"/api/posts/{vid}/playback", {},
                      token=_token_for_read())


def _cmd_api_video(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    return _edge_call("GET", f"/api/videos/{vid}", token=_token_for_read())


def _cmd_api_play(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    ep = _int_arg(args, "ep", "ep", 1)
    token = _token_for_read()
    new_data, new_err = None, None
    try:
        new_data = _normalize_play_info(_edge_call(
            "POST", f"/api/videos/{vid}/episodes/{ep}/playback", {}, token=token))
    except TauriError as e:
        new_err = e
    if new_data and new_data.get("video_url"):
        return new_data
    old_data, old_err = None, None
    try:
        old_data = _normalize_play_info(_edge_call(
            "GET", f"/api/videos/{vid}/play?ep={ep}&page_texts=0", token=token))
    except TauriError as e:
        old_err = e
    if old_data and old_data.get("video_url"):
        return old_data
    if old_data is not None:
        return old_data  # 新接口已失败时返回旧接口结果
    if new_err is not None:
        raise new_err
    if old_err is not None:
        raise old_err
    return new_data


def _cmd_api_comments(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    ep = _int_arg(args, "ep", "ep", 1)
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    return _edge_call(
        "GET", f"/api/videos/{vid}/comments?ep={ep}&page={page}&size={size}",
        token=_token_for_read())


def _cmd_api_post_comments(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    return _edge_call("GET", f"/api/posts/{vid}/comments?page={page}&size={size}",
                      token=_token_for_read())


def _cmd_api_post_categories(args, ctx):
    return _edge_call("GET", "/api/site/categories/post", token=_token_for_read())


def _cmd_api_post_category(args, ctx):
    route = str(_arg(args, "route", "route", ""))
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    sort = _arg(args, "sort", "sort")
    suffix = f"&sort={urllib.parse.quote(str(sort), safe='')}" if sort else ""
    # 已知偏差：remen-chigua 的 SSR 抓取跳过，直接走 JSON。
    return _edge_call(
        "GET", f"/api/posts/category/{urllib.parse.quote(route, safe='')}"
               f"?page={page}&size={size}{suffix}",
        token=_token_for_read())


def _cmd_api_related(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    return _edge_call("GET", f"/api/videos/{vid}/related", token=_token_for_read())


def _cmd_api_user(args, ctx):
    uid = _int_arg(args, "id", "id", 0)
    return _edge_call("GET", f"/api/users/{uid}", token=_token_for_read())


def _cmd_api_user_stats(args, ctx):
    uid = _int_arg(args, "id", "id", 0)
    return _edge_call("GET", f"/api/users/{uid}/stats", token=_token_for_read())


def _cmd_api_user_videos(args, ctx):
    uid = _int_arg(args, "id", "id", 0)
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    return _edge_call("GET", f"/api/videos/user/{uid}?page={page}&size={size}",
                      token=_token_for_read())


def _cmd_api_following(args, ctx):
    sess = _account_session(args)  # 需登录，前端不传 identity，只校验有会话
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 20))
    return _edge_call("GET", f"/api/videos/following?page={page}&size={size}",
                      token=sess.get("token"))


# ---- P2 账号与互动 ----

def _cmd_api_login(args, ctx):
    username = str(_arg(args, "username", "username", ""))
    password = str(_arg(args, "password", "password", ""))
    exp_uid = _arg(args, "expectedUserId", "expected_user_id")
    exp_org = _arg(args, "expectedOrigin", "expected_origin")
    cred_org = _arg(args, "credentialOrigin", "credential_origin")
    provided = [v is not None for v in (exp_uid, exp_org, cred_org)]
    reauth = any(provided)
    if reauth and not all(provided):
        raise TauriError("自动重新登录缺少账号或线路信息")
    base_before = _get_base()
    if reauth:
        sess = _load_session()
        ok = (
            sess and sess.get("token") and sess.get("user")
            and int((sess["user"] or {}).get("id") or 0) == int(exp_uid)
            and sess.get("origin") == exp_org
            and (cred_org == exp_org
                 or (_trusted_auth_origin(cred_org) and _trusted_auth_origin(exp_org)))
        )
        if not ok:
            raise TauriError("登录信息不适用于当前线路，请手动重新登录")
    data = _edge_call("POST", "/api/account/loginByPassword",
                      {"username": username, "password": password}, token=None)
    if _get_base() != base_before:
        raise TauriError("登录期间线路已变化，请重新登录")
    if not isinstance(data, dict) or not data.get("token"):
        raise TauriError("登录返回缺少 token")
    snap = _normalize_account(data, _get_base())
    if reauth and int(snap["user"]["id"]) != int(exp_uid):
        raise TauriError("当前线路返回的账号与本地不一致，请手动重新登录")
    _save_session({"origin": _get_base(), "token": data["token"], "user": snap["user"]})
    return snap


def _cmd_api_register(args, ctx):
    username = str(_arg(args, "username", "username", ""))
    password = str(_arg(args, "password", "password", ""))
    email = str(_arg(args, "email", "email", "")).strip()
    rev_before = _session_revision()
    body = {"username": username, "password": password, "confirm_password": password}
    if email:
        body["email"] = email
    data = _edge_call("POST", "/api/account/registerByPassword", body, token=None)
    if _session_revision() != rev_before:
        raise TauriError("注册期间账号状态已变化，请重试")
    if not isinstance(data, dict) or not data.get("token"):
        raise TauriError("注册返回缺少 token")
    snap = _normalize_account(data, _get_base())
    _save_session({"origin": _get_base(), "token": data["token"], "user": snap["user"]})
    return snap


def _cmd_api_logout(args, ctx):
    sess = _account_session(args)
    rev_before = _session_revision()
    try:
        _edge_call("POST", "/api/account/logout", None, token=sess.get("token"))
    except TauriError:
        pass  # 上游登出失败被忽略（account.rs）
    if _session_revision() != rev_before:
        raise TauriError("账号已变化，请重试")
    _clear_session()
    return {"ok": True}


def _cmd_api_me(args, ctx):
    sess = _account_session(args)
    data = _edge_call("GET", "/api/account/me", token=sess.get("token"))
    return _normalize_account(data, _get_base())


def _cmd_api_account_history(args, ctx):
    sess = _account_session(args, require_identity=True)
    page = _clamp_page(_arg(args, "page", "page", 1))
    size = _clamp_size(_arg(args, "size", "size", 50))
    data = _edge_call("GET", f"/api/users/me/history?page={page}&size={size}",
                      token=sess.get("token"))
    if not isinstance(data, dict):
        raise TauriError("观看历史响应无效")
    items = data.get("items")
    if not isinstance(items, list):
        raise TauriError("观看历史响应无效")
    pg = data.get("pagination")
    if not isinstance(pg, dict) or any(k not in pg for k in ("page", "pages", "size", "total")):
        raise TauriError("观看历史分页信息无效")
    clean = []
    for row in items:
        if not isinstance(row, dict) or row.get("target_type") != "video":
            continue
        try:
            rid, vid = int(row.get("id") or 0), int(row.get("video_id") or 0)
        except (TypeError, ValueError):
            continue
        if rid <= 0 or vid <= 0:
            continue
        try:
            last_ep = max(1, int(row.get("last_ep") or 1))
        except (TypeError, ValueError):
            last_ep = 1
        clean.append({"id": rid, "video_id": vid, "target_type": "video",
                      "last_ep": last_ep,
                      "updated_at": str(row.get("updated_at") or "")})
    return {"items": clean,
            "pagination": {"page": pg["page"], "pages": pg["pages"],
                           "size": pg["size"], "total": pg["total"]}}


def _cmd_api_account_history_record(args, ctx):
    sess = _account_session(args, require_identity=True)
    vid = _int_arg(args, "videoId", "video_id", 0)
    ep = _int_arg(args, "ep", "ep", 0)
    if vid <= 0 or ep == 0:
        raise TauriError("观看记录参数无效")
    return _edge_call("POST", "/api/users/me/history",
                      {"video_id": vid, "ep": ep}, token=sess.get("token"))


def _cmd_api_account_history_delete(args, ctx):
    sess = _account_session(args, require_identity=True)
    ids = _arg(args, "ids", "ids", [])
    if (not isinstance(ids, list) or not ids or len(ids) > 200
            or any(not isinstance(i, int) or i <= 0 for i in ids)):
        raise TauriError("观看记录参数无效")
    return _edge_call("POST", "/api/users/me/history/delete",
                      {"ids": ids}, token=sess.get("token"))


def _cmd_interact(args, ctx, path_fn):
    sess = _account_session(args)
    on = bool(_arg(args, "on", "on", False))
    method = "POST" if on else "DELETE"
    return _edge_call(method, path_fn(args), None, token=sess.get("token"))


def _cmd_api_like(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    ep = _int_arg(args, "ep", "ep", 1)
    return _cmd_interact(args, ctx, lambda a: f"/api/videos/{vid}/like?ep={ep}")


def _cmd_api_collect(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    return _cmd_interact(args, ctx, lambda a: f"/api/videos/{vid}/collect")


def _cmd_api_follow(args, ctx):
    uid = _int_arg(args, "uid", "uid", 0)
    return _cmd_interact(args, ctx, lambda a: f"/api/users/{uid}/follow")


def _cmd_api_post_like(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    return _cmd_interact(args, ctx, lambda a: f"/api/posts/{vid}/like")


def _cmd_api_post_collect(args, ctx):
    vid = _int_arg(args, "id", "id", 0)
    return _cmd_interact(args, ctx, lambda a: f"/api/posts/{vid}/collect")


def _cmd_api_comment_send(args, ctx):
    sess = _account_session(args)  # 需登录，前端不传 identity
    vid = _int_arg(args, "id", "id", 0)
    ep = _int_arg(args, "ep", "ep", 1)
    text = str(_arg(args, "text", "text", ""))
    reply_id = _arg(args, "replyId", "reply_id")
    # replyId > 0 时路径与 body 都分叉（mapping §7 坑 7）
    if reply_id is not None and int(reply_id) > 0:
        return _edge_call("POST", f"/api/videos/{vid}/comments",
                          {"text": text, "reply_id": int(reply_id)},
                          token=sess.get("token"))
    return _edge_call("POST", f"/api/videos/{vid}/comments?ep={ep}",
                      {"text": text}, token=sess.get("token"))


# ---- P3 线路与代理 ----

def _cmd_line_state(args, ctx):
    line = _load_line()
    return {"base": line["base"], "default": DEFAULT_BASE,
            "fixed": [dict(FIXED_LINE)], "custom": line["custom"]}


def _cmd_line_set(args, ctx):
    norm = _normalize_base(_arg(args, "url", "url", ""))
    if not norm:
        raise TauriError("线路地址无效（需要纯 HTTPS 站点地址，禁止内网）")
    if not (_is_fixed(norm) or _is_trusted_mirror(norm)):
        raise TauriError("线路不在固定线路或可信发布页清单中")
    _apply_base(norm, custom=False)
    return norm  # 直接返回字符串，不是对象


def _cmd_line_custom_set(args, ctx):
    norm = _normalize_base(_arg(args, "url", "url", ""))
    if not norm:
        raise TauriError("线路地址无效（需要纯 HTTPS 站点地址，禁止内网）")
    _save_line(norm, custom=True)
    _clear_session()  # 自定义线路无条件清空会话
    return {"base": norm, "custom": True}


def _cmd_line_reset(args, ctx):
    _apply_base(DEFAULT_BASE, custom=False)
    return {"base": DEFAULT_BASE, "custom": False}


def _cmd_line_cancel(args, ctx):
    LINE_GENERATION["n"] += 1
    return None


def _cmd_line_mirrors(args, ctx):
    return _fetch_mirrors()


def _cmd_line_probe(args, ctx):
    urls = _arg(args, "urls", "urls", [])
    if not isinstance(urls, list):
        urls = []
    out = []
    for chunk in [urls[i:i + 6] for i in range(0, min(len(urls), MIRROR_MAX), 6)]:
        with ThreadPoolExecutor(max_workers=6) as pool:
            hits = list(pool.map(lambda u: _probe_hit("mirror", u), chunk))
        for hit in hits:
            out.append({"url": hit["url"], "ok": hit["ok"], "status": hit["status"],
                        "ms": hit["ms"], **({"error": hit["error"]} if hit.get("error") else {})})
    return out


def _cmd_line_auto(args, ctx):
    gen = LINE_GENERATION["n"]
    previous = _get_base()
    main_hit = _probe_hit("main", DEFAULT_BASE)
    candidates = [{"tier": "main", "url": DEFAULT_BASE, "ok": main_hit["ok"],
                   "reachable": main_hit["reachable"], "ms": main_hit["ms"],
                   "status": main_hit["status"]}]
    pick = None
    mirror_error = None
    if main_hit["ok"]:
        pick = {"base": DEFAULT_BASE, "tier": "main", "ms": main_hit["ms"],
                "reason": "main-ok", "candidates": candidates}
    else:
        try:
            listing = _fetch_mirrors()
            urls = [it["url"] for it in listing["items"]
                    if not urllib.parse.urlparse(it["url"]).hostname.endswith(".ai")][:10]
            with ThreadPoolExecutor(max_workers=6) as pool:
                hits = list(pool.map(lambda u: _probe_hit("mirror", u), urls))
            for hit in hits:
                candidates.append({"tier": "mirror", "url": hit["url"], "ok": hit["ok"],
                                   "reachable": hit["reachable"], "ms": hit["ms"],
                                   "status": hit["status"]})
            ok_hits = [h for h in hits if h["ok"]]
            if ok_hits:
                best = min(ok_hits, key=lambda h: h["ms"])
                pick = {"base": best["url"], "tier": "mirror", "ms": best["ms"],
                        "reason": "mirror-ok", "candidates": candidates}
        except TauriError as e:
            mirror_error = str(e)
    if pick is None:
        pick = {"base": "", "tier": "none", "ms": main_hit["ms"],
                "reason": "all-down", "candidates": candidates}
    cancelled = LINE_GENERATION["n"] != gen
    switched = False
    base = previous
    if not cancelled and pick["base"] and pick["base"] != previous:
        _apply_base(pick["base"], custom=False)
        base = pick["base"]
        switched = True
    result = {"previous": previous, "base": base, "switched": switched, "pick": pick}
    if cancelled:
        result["cancelled"] = True
    if mirror_error:
        result["mirror_error"] = mirror_error
    return result


def _cmd_line_try_main(args, ctx):
    gen = LINE_GENERATION["n"]
    previous = _get_base()
    hit = _probe_hit("main", DEFAULT_BASE)
    switched = False
    base = previous
    if LINE_GENERATION["n"] == gen and hit["ok"] and previous != DEFAULT_BASE:
        _apply_base(DEFAULT_BASE, custom=False)
        base = DEFAULT_BASE
        switched = True
    probe = {"ok": hit["ok"], "base": DEFAULT_BASE, "tier": "main",
             "url": hit["url"], "reachable": hit["reachable"],
             "ms": hit["ms"], "status": hit["status"]}
    if hit.get("error"):
        probe["error"] = hit["error"]
    result = {"previous": previous, "base": base, "switched": switched, "probe": probe}
    if LINE_GENERATION["n"] != gen:
        result["cancelled"] = True
    return result


def _cmd_line_health(args, ctx):
    base = _get_base()
    hit = _probe_hit("current", base)
    return {"base": base, "ok": hit["ok"], "ms": hit["ms"], "status": hit["status"]}


def _redact_proxy_url(url):
    if not url:
        return url
    try:
        p = urllib.parse.urlparse(url)
        if p.password:
            host = p.hostname or ""
            port = f":{p.port}" if p.port else ""
            return f"{p.scheme}://{p.username}:***@{host}{port}{p.path or ''}"
        return url
    except ValueError:
        return url


def _proxy_state_value():
    st = _load_proxy()
    env = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
           or os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") or "")
    if st["mode"] == "custom":
        effective = _redact_proxy_url(st["url"]) if st["url"] else None
    elif st["mode"] == "off":
        effective = None
    else:
        effective = _redact_proxy_url(env) if env else "system"
    return {"mode": st["mode"], "url": st["url"], "effective": effective,
            "system_proxy": bool(env), "env_proxy": bool(env)}


def _cmd_proxy_state(args, ctx):
    return _proxy_state_value()


def _cmd_proxy_set(args, ctx):
    mode = str(_arg(args, "mode", "mode", "system"))
    url = str(_arg(args, "url", "url", "")).strip()
    if mode not in ("system", "custom", "off"):
        raise TauriError("代理模式无效")
    if mode == "custom":
        try:
            p = urllib.parse.urlparse(url)
        except ValueError:
            raise TauriError("代理地址无效")
        if p.scheme not in ("http", "https") or not p.hostname:
            raise TauriError("代理地址无效")
    _save_proxy(mode, url if mode == "custom" else "")
    return _proxy_state_value()


def _cmd_proxy_test(args, ctx):
    mode = str(_arg(args, "mode", "mode", "system"))
    url = str(_arg(args, "url", "url", "")).strip()
    started = time.time()
    if mode not in ("system", "custom", "off"):
        return {"ok": False, "ms": 0, "error": "代理模式无效"}
    if mode == "custom":
        try:
            p = urllib.parse.urlparse(url)
        except ValueError:
            return {"ok": False, "ms": 0, "error": "代理地址无效"}
        if p.scheme not in ("http", "https") or not p.hostname:
            return {"ok": False, "ms": 0, "error": "代理地址无效"}
    if mode == "off":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    elif mode == "custom":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": url, "https": url}))
    else:
        opener = urllib.request.build_opener()
    via = _redact_proxy_url(url) if mode == "custom" else ("system" if mode == "system" else None)
    target = _get_base().rstrip("/") + "/api/videos?page=1&size=1"
    try:
        req = urllib.request.Request(target, method="GET")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        with opener.open(req, timeout=8) as resp:
            raw = resp.read(1024 * 1024 + 1)
            status = resp.status
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "ms": int((time.time() - started) * 1000),
                "error": f"HTTP {e.code}", "via": via}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - started) * 1000),
                "error": f"请求失败：{e}", "via": via}
    ms = int((time.time() - started) * 1000)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"ok": False, "status": status, "ms": ms,
                "error": "接口返回失败", "via": via}
    ok = (status == 200 and isinstance(payload, dict) and payload.get("status") == 1
          and _healthy_video_data(payload.get("data")))
    result = {"ok": ok, "status": status, "ms": ms, "via": via}
    if not ok:
        result["error"] = "接口未返回有效视频列表数据"
    return result


# ---- P4 no-op stub ----

def _cmd_check_update(args, ctx):
    # 任务要求：固定返回 has_update=false，不调 GitHub。
    current = str(_arg(args, "currentVersion", "current_version") or "2.2.8")
    return {"asset": None, "current": current, "latest": current,
            "has_update": False, "url": "https://github.com/zxsos/HGhubX/releases"}


def _cmd_open_url(args, ctx):
    url = str(_arg(args, "url", "url", ""))
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        raise TauriError("链接无效")
    if (p.scheme not in ("http", "https") or p.username or p.password
            or any(ord(c) < 32 for c in url)):
        raise TauriError("链接无效")
    return None  # Web 端由前端 window.open(url, "_blank") 打开


def _cmd_set_orientation(args, ctx):
    return None


def _cmd_set_text_interaction(args, ctx):
    return None


def _cmd_download_tv_update(args, ctx):
    # TV 专属，Web 端无等价物：明确报错，不假装成功。
    raise TauriError("TV 版专属功能，Web 端不支持")


def _cmd_cancel_tv_update(args, ctx):
    return None


def _cmd_cache_clear(args, ctx):
    IMG_CACHE.store.clear()
    PL_CACHE.clear()
    SEG_CACHE.clear()
    # 注意：字段是 camelCase（webCache），其他命令保持 snake_case。
    return {"webCache": False, "platform": "web"}


def _cmd_net_diag(args, ctx):
    base = _get_base()
    host = urllib.parse.urlparse(base).hostname or ""
    env_proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                 or os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") or "")
    dns, tcp, http = {"ok": False}, {"ok": False}, {"ok": False}
    try:
        addrs = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        dns = {"ok": True, "addrs": sorted({a[4][0] for a in addrs})}
    except OSError as e:
        dns = {"ok": False, "error": str(e)}
    try:
        started = time.time()
        with socket.create_connection((host, 443), timeout=5):
            tcp = {"ok": True, "ms": int((time.time() - started) * 1000)}
    except OSError as e:
        tcp = {"ok": False, "error": str(e)}
    try:
        started = time.time()
        status, _final, _raw, _ct = _raw_request(
            "GET", base.rstrip("/") + "/", None, {"Accept": "text/html"}, timeout=20)
        http = {"ok": status == 200, "status": status,
                "ms": int((time.time() - started) * 1000),
                "proxy_configured": bool(env_proxy)}
    except TauriError as e:
        http = {"ok": False, "error": str(e), "proxy_configured": bool(env_proxy)}
    if http.get("ok"):
        verdict, summary, advice = "ok", "网络正常", ""
    elif tcp.get("ok"):
        verdict, summary = "degraded", "TCP 可达但 HTTP 异常", "请稍后重试或在设置中切换线路"
    else:
        verdict, summary = "failed", "无法连接上游", "请检查网络后重试"
    return {"dns": dns, "tcp": tcp, "http": http,
            "proxy": {"env_proxy": _redact_proxy_url(env_proxy) if env_proxy else None,
                      "mode": _load_proxy()["mode"]},
            "verdict": verdict, "summary": summary, "advice": advice}


def _playback_log_status_value():
    return {"enabled": PLAYBACK_LOG["enabled"],
            "path": PLAYBACK_LOG_FILE if PLAYBACK_LOG["initialized"] else None,
            "error": None}


def _cmd_playback_log_status(args, ctx):
    return _playback_log_status_value()


def _cmd_playback_log_set(args, ctx):
    enabled = bool(_arg(args, "enabled", "enabled", False))
    PLAYBACK_LOG["enabled"] = enabled
    if enabled and not PLAYBACK_LOG["initialized"]:
        try:
            with open(PLAYBACK_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": "env", "platform": "web"}, ensure_ascii=False) + "\n")
            PLAYBACK_LOG["initialized"] = True
        except OSError:
            pass
    return _playback_log_status_value()


def _cmd_playback_log_write(args, ctx):
    entries = _arg(args, "entries", "entries", [])
    if not isinstance(entries, list):
        raise TauriError("日志条目无效")
    if len(entries) > 128:
        raise TauriError("日志批次过大")
    for entry in entries:
        if not isinstance(entry, dict):
            raise TauriError("日志条目无效")
        event = entry.get("event")
        if (not isinstance(event, str) or len(event) > 80
                or not re.fullmatch(r"[A-Za-z0-9._-]+", event)):
            raise TauriError("日志条目无效")
        if len(json.dumps(entry.get("data"), ensure_ascii=False)) > 8192:
            raise TauriError("日志条目无效")
    if not PLAYBACK_LOG["enabled"]:
        return None
    try:
        if os.path.exists(PLAYBACK_LOG_FILE) and os.path.getsize(PLAYBACK_LOG_FILE) > 2 * 1024 * 1024:
            os.replace(PLAYBACK_LOG_FILE, PLAYBACK_PREV_LOG_FILE)
        with open(PLAYBACK_LOG_FILE, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps({"event": "web." + entry["event"],
                                    "data": entry.get("data")}, ensure_ascii=False) + "\n")
        PLAYBACK_LOG["initialized"] = True
    except OSError:
        pass
    return None


def _cmd_playback_log_read(args, ctx):
    if not os.path.exists(PLAYBACK_LOG_FILE):
        raise TauriError("日志目录尚未初始化")
    try:
        with open(PLAYBACK_LOG_FILE, "rb") as f:
            data = f.read()
    except OSError as e:
        raise TauriError(str(e))
    return data[-256 * 1024:].decode("utf-8", "ignore")


TAURI_COMMANDS = {
    # P0 启动必需
    "app_boot": _cmd_app_boot,
    "media_http_origin": _cmd_media_http_origin,
    "security_set": _cmd_security_set,
    "debug_log": _cmd_debug_log,
    # P1 内容只读（25）
    "api_feed": _cmd_api_feed,
    "api_topics": _cmd_api_topics,
    "api_topic": _cmd_api_topic,
    "api_tags": _cmd_api_tags,
    "api_tag_videos": _cmd_api_tag_videos,
    "api_post_tag": _cmd_api_post_tag,
    "api_category": _cmd_api_category,
    "api_original_videos": _cmd_api_original_videos,
    "api_rank": _cmd_api_rank,
    "api_search": _cmd_api_search,
    "api_search_hot": _cmd_api_search_hot,
    "api_posts": _cmd_api_posts,
    "api_post": _cmd_api_post,
    "api_post_playback": _cmd_api_post_playback,
    "api_video": _cmd_api_video,
    "api_play": _cmd_api_play,
    "api_comments": _cmd_api_comments,
    "api_post_comments": _cmd_api_post_comments,
    "api_post_categories": _cmd_api_post_categories,
    "api_post_category": _cmd_api_post_category,
    "api_related": _cmd_api_related,
    "api_user": _cmd_api_user,
    "api_user_stats": _cmd_api_user_stats,
    "api_user_videos": _cmd_api_user_videos,
    "api_following": _cmd_api_following,
    # P2 账号与互动（13）
    "api_login": _cmd_api_login,
    "api_register": _cmd_api_register,
    "api_logout": _cmd_api_logout,
    "api_me": _cmd_api_me,
    "api_account_history": _cmd_api_account_history,
    "api_account_history_record": _cmd_api_account_history_record,
    "api_account_history_delete": _cmd_api_account_history_delete,
    "api_comment_send": _cmd_api_comment_send,
    "api_like": _cmd_api_like,
    "api_collect": _cmd_api_collect,
    "api_follow": _cmd_api_follow,
    "api_post_like": _cmd_api_post_like,
    "api_post_collect": _cmd_api_post_collect,
    # P3 线路与代理（13）
    "line_state": _cmd_line_state,
    "line_set": _cmd_line_set,
    "line_custom_set": _cmd_line_custom_set,
    "line_reset": _cmd_line_reset,
    "line_cancel": _cmd_line_cancel,
    "line_mirrors": _cmd_line_mirrors,
    "line_probe": _cmd_line_probe,
    "line_auto": _cmd_line_auto,
    "line_try_main": _cmd_line_try_main,
    "line_health": _cmd_line_health,
    "proxy_state": _cmd_proxy_state,
    "proxy_set": _cmd_proxy_set,
    "proxy_test": _cmd_proxy_test,
    # P4 no-op stub（12）
    "check_update": _cmd_check_update,
    "open_url": _cmd_open_url,
    "set_orientation": _cmd_set_orientation,
    "set_text_interaction": _cmd_set_text_interaction,
    "download_tv_update": _cmd_download_tv_update,
    "cancel_tv_update": _cmd_cancel_tv_update,
    "cache_clear": _cmd_cache_clear,
    "net_diag": _cmd_net_diag,
    "playback_log_status": _cmd_playback_log_status,
    "playback_log_set": _cmd_playback_log_set,
    "playback_log_write": _cmd_playback_log_write,
    "playback_log_read": _cmd_playback_log_read,
}


def _dispatch_tauri(cmd, args, ctx):
    """分发一条 Tauri 命令；未知命令按 Tauri 原文报错。
    tv_migration_complete 未在 lib.rs 注册，前端捕获该字符串后忽略。"""
    if cmd == "tv_migration_complete":
        raise TauriError("Command tv_migration_complete not found")
    fn = TAURI_COMMANDS.get(cmd)
    if fn is None:
        raise TauriError(f"Command {cmd} not found")
    if not isinstance(args, dict):
        args = {}
    return fn(args, ctx)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-cdn-auth")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json")

    def _proxy_to(self, url, data=None, headers=None):
        status, body, ctype = _fetch(url, data=data, headers=headers)
        self._send(status, body, content_type=ctype or "application/octet-stream")

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            # 方案 B：有 dist/ 就 serving HGhubX 新版前端，否则回落旧版单文件页。
            index_path = DIST_INDEX_PATH if os.path.isfile(DIST_INDEX_PATH) else INDEX_PATH
            try:
                with open(index_path, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._json(404, {"status": 0, "msg": "index.html not found"})
            return
        if path == "/legacy":
            # 旧版单文件前端（回滚对照用）：新版出问题时可临时从这里打开旧版。
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._json(404, {"status": 0, "msg": "index.html not found"})
            return
        if path.startswith("/assets/"):
            # 方案 B：只 serving dist/assets/ 下的构建产物，路径穿越直接 404。
            name = path[len("/assets/"):]
            if not name or "/" in name or name.startswith("."):
                self._json(404, {"status": 0, "msg": "asset not found"})
                return
            asset_path = os.path.join(DIST_ASSETS_DIR, name)
            if not os.path.isfile(asset_path):
                self._json(404, {"status": 0, "msg": "asset not found"})
                return
            ctype = "application/octet-stream"
            if name.endswith(".js"):
                ctype = "application/javascript; charset=utf-8"
            elif name.endswith(".css"):
                ctype = "text/css; charset=utf-8"
            elif name.endswith(".svg"):
                ctype = "image/svg+xml"
            try:
                with open(asset_path, "rb") as f:
                    self._send(200, f.read(), ctype)
            except FileNotFoundError:
                self._json(404, {"status": 0, "msg": "asset not found"})
            return
        if path == "/hls.min.js":
            # hls.js 本地化（1.2.0.2）：不再依赖 jsdelivr CDN，APK 内也用同一份。
            try:
                with open(os.path.join(BASE_DIR, "hls.min.js"), "rb") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except FileNotFoundError:
                self._json(404, {"status": 0, "msg": "hls.min.js not found"})
            return
        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path in ("/proxy/m3u8", "/proxy/key", "/proxy/ts"):
            url = (qs.get("url") or [""])[0]
            if not url:
                self._json(400, {"status": 0, "msg": "missing url"})
                return
            self._proxy_to(PROXY_BASE + path + "?url=" + urllib.parse.quote(url, safe=""))
            return
        if path == "/proxy/img":
            self._proxy_img((qs.get("url") or [""])[0])
            return
        if path == "/api/prefip":
            isp = (qs.get("isp") or ["auto"])[0]
            all_lists = _prefip_all()
            if isp in ("ct", "cu", "cmcc"):
                self._json(200, {"status": 1, "data": {"isp": isp, "ips": all_lists.get(isp, [])}})
            else:
                self._json(200, {"status": 1, "data": {"isp": "auto", "ips": all_lists.get("ct", []), "ct": all_lists.get("ct", []), "cu": all_lists.get("cu", []), "cmcc": all_lists.get("cmcc", [])}})
            return
        if path in ("/img", "/hls", "/seg"):
            # HGhubX 媒体路由（同源替代 hg:// / hg.localhost / 127.0.0.1:17650）
            u = (qs.get("u") or [""])[0]
            if path == "/img":
                self._handle_img(u)
            elif path == "/hls":
                self._handle_hls(u)
            else:
                self._handle_seg(u)
            return
        if path.startswith("/api/up/"):
            rest = path[len("/api/up/"):]
            target = f"{UPSTREAM}/api/{rest}"
            if parsed.query:
                target += "?" + parsed.query
            self._proxy_to(target)
            return
        if path.startswith("/api/edge/"):
            target = UPSTREAM + path
            if parsed.query:
                target += "?" + parsed.query
            self._proxy_to(target)
            return
        if path.startswith("/api/"):
            # 1.2.0.6：直接的 /api/* 同源代理。前端概念上调的就是 /api/videos、
            # /api/search、/api/ranks、/api/posts、/api/site/*、/api/videos/:id、
            # /api/videos/:id/comments，之前只有 /api/up/* 路由，直接调 /api/*
            # 会走到文末 404；前端 api() 的兜底又是跨域直连上游，手机网络下经常
            # 挂起/CORS 挡住。现在 /api/* 一律由本站反代到上游，不再跨域。
            target = UPSTREAM + path
            if parsed.query:
                target += "?" + parsed.query
            self._proxy_to(target)
            return
        self._json(404, {"status": 0, "msg": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/tauri":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
                cmd = payload.get("cmd")
                args = payload.get("args") or {}
            except Exception:
                self._json(400, {"ok": False, "error": "请求失败，请检查网络"})
                return
            ctx = {"host": self.headers.get("Host"),
                   "proto": self.headers.get("X-Forwarded-Proto") or "http"}
            try:
                data = _dispatch_tauri(cmd, args, ctx)
                # 直接返回命令结果本身：Tauri 的 invoke 收到什么，前端就拿到什么，
                # 不包 {"ok": True, "data": ...} 这一层。之前包了一层，前端把
                # 整个信封当成命令返回值，items 读不到就全页空白，搜索页直接白屏。
                self._json(200, data)
            except TauriError as e:
                self._json(500, {"error": str(e)})
            except Exception as e:
                log.warning("tauri %s failed: %s", cmd, e)
                self._json(500, {"error": "请求失败，请检查网络"})
            return
        if parsed.path == "/api/play":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            self._proxy_to(PROXY_BASE + "/api/play", data=body,
                           headers={"Content-Type": "application/json"})
            return
        if parsed.path.startswith("/api/up/"):
            # 1.2.0.4 评论发布：POST /api/up/videos/:id/comments 透传到上游
            rest = parsed.path[len("/api/up/"):]
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            self._proxy_to(f"{UPSTREAM}/api/{rest}", data=body,
                           headers={"Content-Type": "application/json"})
            return
        if parsed.path.startswith("/api/edge/"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            self._proxy_to(UPSTREAM + parsed.path, data=body,
                           headers={"Content-Type": "application/json"})
            return
        if parsed.path.startswith("/api/"):
            # 1.2.0.6：POST 的 /api/* 也同源反代到上游（与 GET 的通用路由对应，
            # 例如直接 POST /api/videos/:id/comments；/api/play 在前面已处理）。
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            self._proxy_to(UPSTREAM + parsed.path, data=body,
                           headers={"Content-Type": "application/json"})
            return
        self._json(404, {"status": 0, "msg": "not found"})

    def _proxy_img(self, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            self._json(400, {"status": 0, "msg": "url must be http(s)"})
            return
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host not in IMG_HOSTS:
            self._json(403, {"status": 0, "msg": f"image host not allowed: {host}"})
            return
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        hit = IMG_CACHE.get(cache_key)
        if hit is not None:
            self._send(200, hit[0], content_type=hit[1])
            return
        status, body, _ = _fetch(url, timeout=20)
        if status != 200:
            self._json(status, {"status": 0, "msg": f"upstream HTTP {status}"})
            return
        # 绝大多数封面是 AES 加密字节：先解密，解密出合法图片就返回图片；
        # 已经是明文图片（或解密失败）就原样返回，浏览器自己判断。
        plain = _aes_decrypt_cbc_nopad(body)
        if plain is not None and _sniff_image(plain) != "application/octet-stream":
            ctype = _sniff_image(plain)
            IMG_CACHE.put(cache_key, (plain, ctype))
            self._send(200, plain, content_type=ctype)
            return
        ctype = _sniff_image(body)
        if ctype == "application/octet-stream":
            ctype = "image/jpeg"
        IMG_CACHE.put(cache_key, (body, ctype))
        self._send(200, body, content_type=ctype)

    # ---- HGhubX 媒体路由（/img /hls /seg，移植自 media/{image,hls,segment,response}.rs） ----

    def _media_send(self, status, body, ctype, cache="no-store", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Length, Content-Type, Accept-Ranges, Content-Range")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _media_502(self, msg):
        self._media_send(502, msg, "text/plain", cache="no-store")

    def _handle_img(self, url):
        """封面：拉取 -> 明文直返 / AES 解密 -> 魔数校验；失败 502，绝不 200 空内容。"""
        if not url:
            self._media_send(400, "missing u", "text/plain")
            return
        if not url.startswith(("http://", "https://")):
            self._media_send(400, "missing u", "text/plain")
            return
        if not _media_url_allowed(url):
            self._media_502("上游拉取失败")
            return
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        hit = IMG_CACHE.get(cache_key)
        if hit is not None:
            self._media_send(200, hit[0], hit[1], cache="no-store")
            return
        try:
            status, _final, body, _ct = _raw_request(
                "GET", url, None, {"Accept": "image/*,*/*"}, timeout=20)
        except TauriError as e:
            self._media_502(str(e))
            return
        if status != 200:
            self._media_502(f"上游 HTTP {status}")
            return
        plain = body
        if _sniff_image(plain) == "application/octet-stream":
            dec = _aes_decrypt_cbc_nopad(body)  # 复用 /proxy/img 的解密逻辑
            if dec is None:
                self._media_502("封面解密失败")
                return
            plain = _strip_pkcs7(dec)
        kind = _sniff_image(plain)
        if kind == "application/octet-stream":
            self._media_502("封面格式无效")
            return
        IMG_CACHE.put(cache_key, (plain, kind))
        self._media_send(200, plain, kind, cache="no-store")

    def _handle_hls(self, url):
        """清单：拉取 -> UTF-8/#EXTM3U 校验 -> 改写为本地 /seg?u=；Range 不截断。"""
        if not url:
            self._media_send(400, "missing u", "text/plain")
            return
        if not _media_url_allowed(url):
            self._media_502("上游拉取失败")
            return
        hit = _cache_get(PL_CACHE, url, 60)
        if hit is not None:
            self._media_send(200, hit, "application/vnd.apple.mpegurl", cache="no-store")
            return
        try:
            status, final_url, body, _ct = _raw_request(
                "GET", url, None, {"Accept": "application/vnd.apple.mpegurl,*/*"}, timeout=25)
        except TauriError as e:
            self._media_502(str(e))
            return
        if status != 200:
            self._media_502(f"上游 HTTP {status}")
            return
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            self._media_502("播放列表不是 UTF-8 文本")
            return
        if not text.lstrip("\ufeff").startswith("#EXTM3U"):
            self._media_502("响应不是 HLS 播放列表")
            return
        # 分片/密钥直连 CDN，不再经本站中转（2026-11-23 卡顿优化）
        rewritten = _rewrite_playlist(final_url, text, prefix="", direct=True)
        _cache_put(PL_CACHE, url, rewritten, 60)
        self._media_send(200, rewritten, "application/vnd.apple.mpegurl", cache="no-store")

    def _seg_range(self, body):
        """按 response.rs apply_byte_range：合法单段 -> 206；不可满足 -> 416；
        非法/多段/非 bytes 单位 -> 完整 200。返回 (status, body, extra_headers)。"""
        ranges = self.headers.get_all("Range") or []
        if self.headers.get("If-Range") is not None or len(ranges) != 1:
            return 200, body, {"Accept-Ranges": "bytes"}
        spec = ranges[0].strip()
        if not spec.startswith("bytes="):
            return 200, body, {"Accept-Ranges": "bytes"}
        spec = spec[len("bytes="):]
        if "," in spec or "-" not in spec:
            return 200, body, {"Accept-Ranges": "bytes"}
        first, last = spec.split("-", 1)

        def num(s):
            return int(s) if s and s.isdigit() else None

        n = len(body)
        if first == "":
            suffix = num(last)
            if suffix is None:
                return 200, body, {"Accept-Ranges": "bytes"}
            bounds = (max(0, n - suffix), n - 1) if suffix > 0 and n > 0 else None
        else:
            start = num(first)
            if start is None:
                return 200, body, {"Accept-Ranges": "bytes"}
            if last == "":
                end = n - 1
            else:
                end = num(last)
                if end is None or end < start:
                    return 200, body, {"Accept-Ranges": "bytes"}
            bounds = (start, min(end, n - 1)) if start < n else None
        if bounds is None:
            return 416, b"", {"Accept-Ranges": "bytes",
                             "Content-Range": f"bytes */{n}",
                             "Cache-Control": "no-store"}
        s, e = bounds
        return 206, body[s:e + 1], {"Accept-Ranges": "bytes",
                                    "Content-Range": f"bytes {s}-{e}/{n}"}

    def _handle_seg(self, url):
        """分片/密钥/嵌套清单：嵌套清单继续改写；非清单支持单段 Range。"""
        if not url:
            self._media_send(400, "missing u", "text/plain")
            return
        if not _media_url_allowed(url):
            self._media_502("上游拉取失败")
            return
        hit = _cache_get(SEG_CACHE, url, 36)
        if hit is not None:
            ctype, body = hit
            if "mpegurl" in ctype:
                self._media_send(200, body, ctype, cache="no-store")
            else:
                status, out, extra = self._seg_range(body)
                self._media_send(status, out, ctype, cache="no-store", extra=extra)
            return
        try:
            status, _final, body, ctype = _raw_request(
                "GET", url, None, {"Accept": "*/*"}, timeout=20)
        except TauriError as e:
            self._media_502(str(e))
            return
        if status != 200:
            self._media_502(f"上游 HTTP {status}")
            return
        if not ctype:
            path = urllib.parse.urlsplit(url).path.lower()
            if path.endswith(".ts"):
                ctype = "video/mp2t"
            elif path.endswith(".m4s") or path.endswith(".mp4"):
                ctype = "video/mp4"
            else:
                ctype = "application/octet-stream"
        is_playlist = ("mpegurl" in ctype.lower()
                       or body.startswith(b"#EXTM3U")
                       or body.startswith(b"\xef\xbb\xbf#EXTM3U"))
        if is_playlist:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                self._media_502("invalid playlist encoding")
                return
            rewritten = _rewrite_playlist(url, text, prefix="", direct=True)
            self._media_send(200, rewritten, "application/vnd.apple.mpegurl",
                             cache="no-store")
            return
        if len(body) <= 4 * 1024 * 1024:
            _cache_put(SEG_CACHE, url, (ctype, body), 36)
        status, out, extra = self._seg_range(body)
        self._media_send(status, out, ctype, cache="no-store", extra=extra)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("hghub-web listening on %s:%d (proxy -> %s)", HOST, PORT, PROXY_BASE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
