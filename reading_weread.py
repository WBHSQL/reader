from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from threading import RLock, Thread
import time
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup
import requests

WEREAD_BASE = "https://weread.qq.com"
READER_HOME = Path(os.environ.get("READER_HOME", str(Path.home() / ".reader")))
DEFAULT_AUTH_PATH = READER_HOME / "state" / "weread_auth.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class WeReadError(RuntimeError):
    def __init__(self, code: str | int, message: str, *, retriable: bool = True) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retriable = retriable


def _headers(cookie: str = "", *, html: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*" if html else "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": WEREAD_BASE,
        "Referer": WEREAD_BASE + "/",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _raise_payload_error(payload: dict[str, Any]) -> None:
    raw = payload.get("errCode", payload.get("errcode", 0))
    try:
        code = int(raw or 0)
    except (TypeError, ValueError):
        code = 0
    if not code:
        return
    message = str(payload.get("errMsg") or payload.get("errmsg") or code)
    raise WeReadError(code, message, retriable=code not in {-2041, -2012, -2010})


def _cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)


class WeReadAuthStore:
    def __init__(self, path: Path = DEFAULT_AUTH_PATH) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return {}
            return value if isinstance(value, dict) else {}

    def cookie(self) -> str:
        return str(self.load().get("cookie", "")).strip()

    def save(self, *, cookie: str, vid: str = "") -> None:
        data = {"cookie": cookie, "vid": str(vid or ""), "updated_at": time.time()}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                tmp = Path(fh.name)
            tmp.replace(self.path)


class WeReadClient:
    def __init__(self, store: WeReadAuthStore | None = None) -> None:
        self.store = store or WeReadAuthStore()

    def _cookie(self) -> str:
        cookie = self.store.cookie()
        if not cookie:
            raise WeReadError("not_authorized", "微信读书未授权", retriable=False)
        return cookie

    def shelf(self) -> list[dict[str, Any]]:
        cookie = self._cookie()
        response = requests.get(
            WEREAD_BASE + "/web/shelf/sync",
            params={"userVid": "", "synckey": 0}, headers=_headers(cookie), timeout=20,
        )
        if response.status_code != 200:
            raise WeReadError(response.status_code, f"微信读书书架请求 HTTP {response.status_code}")
        payload = response.json()
        _raise_payload_error(payload)
        books = payload.get("books") or (payload.get("data") or {}).get("books") or []
        return [row for row in books if isinstance(row, dict)]

    def search_mp_book(self, account_name: str) -> dict[str, Any] | None:
        cookie = self._cookie()
        wanted = "".join(str(account_name or "").split())
        response = requests.get(
            WEREAD_BASE + "/web/search/global",
            params={"keyword": account_name, "maxIdx": 0, "count": 20},
            headers=_headers(cookie), timeout=20,
        )
        if response.status_code != 200:
            raise WeReadError(response.status_code, f"微信读书搜索请求 HTTP {response.status_code}")
        payload = response.json()
        if isinstance(payload, dict):
            _raise_payload_error(payload)
        books = payload.get("books") or [] if isinstance(payload, dict) else []
        candidates: list[dict[str, Any]] = []
        for item in books:
            if not isinstance(item, dict):
                continue
            info = item.get("bookInfo") or {}
            if not isinstance(info, dict):
                continue
            book_id = str(info.get("bookId", ""))
            if not book_id.startswith("MP_WXS_"):
                continue
            candidates.append(info)
        for row in candidates:
            title = "".join(str(row.get("title") or row.get("name") or "").split())
            if title == wanted:
                return row
        return next((row for row in candidates if wanted and wanted in "".join(str(row.get("title") or row.get("name") or "").split())), None)

    def find_mp_book(self, account_name: str) -> dict[str, Any] | None:
        wanted = "".join(account_name.split())
        try:
            shelf_rows = self.shelf()
        except WeReadError:
            shelf_rows = []
        candidates = [row for row in shelf_rows if str(row.get("bookId", "")).startswith("MP_WXS_")]
        for row in candidates:
            title = "".join(str(row.get("title", "")).split())
            if title == wanted:
                return row
        partial = next((row for row in candidates if wanted and wanted in "".join(str(row.get("title", "")).split())), None)
        if partial is not None:
            return partial
        return self.search_mp_book(account_name)

    def latest_cover(self, book_id: str) -> dict[str, Any]:
        cookie = self._cookie()
        response = requests.get(
            WEREAD_BASE + "/api/mp/cover",
            params={"bookId": book_id}, headers=_headers(cookie), timeout=20,
        )
        if response.status_code != 200:
            raise WeReadError(response.status_code, f"微信读书公众号请求 HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise WeReadError("invalid_cover", "微信读书公众号响应格式异常")
        if not payload.get("reviewId"):
            _raise_payload_error(payload)
            raise WeReadError("empty_cover", "微信读书没有返回该公众号的最新文章", retriable=False)
        return payload

    def article_html(self, review_id: str) -> str:
        cookie = self._cookie()
        response = requests.get(
            WEREAD_BASE + "/web/mp/content",
            params={"reviewId": review_id}, headers=_headers(cookie, html=True), timeout=30,
        )
        if response.status_code != 200:
            raise WeReadError(response.status_code, f"微信读书正文请求 HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "lxml")
        content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
        if content is None:
            raise WeReadError("invalid_content", "微信读书没有返回文章正文")
        for node in content.select("script,style"):
            node.decompose()
        return "\n\n".join(x.strip() for x in content.stripped_strings if x.strip())


def mp_link_from_review(review_id: str, book_id: str) -> str:
    review_id = str(review_id or "").strip()
    if not review_id:
        return ""
    prefix = f"{book_id}_" if book_id else ""
    if prefix and review_id.startswith(prefix):
        token = review_id[len(prefix):]
    elif "_" in review_id:
        token = review_id.split("_")[-1]
    else:
        token = review_id
    return "https://mp.weixin.qq.com/s/" + quote(token, safe="~")


def account_name(source: str) -> str:
    text = str(source or "").replace("微信公众号", "").replace("·", " ")
    return " ".join(text.split()).strip()


DEFAULT_LIVE_STATE_PATH = READER_HOME / "state" / "weread_live_seen.json"
DEFAULT_LIVE_ACCOUNTS = (
    ("jiyichengzai", "微信公众号 · 记忆承载", "记忆承载", "https://www.jintiankansha.com/column/4k0SK6QY1U?type=recent"),
    ("jiyichengzai3", "微信公众号 · 记忆承载3", "记忆承载3", "https://www.jintiankansha.com/column/mQJLCUg4xH"),
    ("qingbian", "微信公众号 · 请辩", "请辩", "https://www.jintiankansha.com/column/QYd1oKfBHb"),
)


class WeReadLiveDiscovery:
    """Use authenticated WeRead cover as the low-frequency real-time discovery source."""

    def __init__(self, *, client: WeReadClient | None = None, state_path: Path = DEFAULT_LIVE_STATE_PATH, accounts=DEFAULT_LIVE_ACCOUNTS) -> None:
        self.client = client or WeReadClient()
        self.state_path = Path(state_path)
        self.accounts = tuple(accounts)
        self._lock = RLock()

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "accounts": {}}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "accounts": {}}
        if not isinstance(value, dict):
            return {"version": 1, "accounts": {}}
        value.setdefault("version", 1); value.setdefault("accounts", {})
        return value

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state_path.parent,
                                         prefix=self.state_path.name + ".", suffix=".tmp", delete=False) as fh:
            fh.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            tmp = Path(fh.name)
        tmp.replace(self.state_path)

    @staticmethod
    def _latest_index_title(url: str) -> str:
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
            for cell in soup.select("div.cell.item"):
                anchor = cell.select_one("span.item_title a[href]")
                if anchor is None:
                    continue
                href = str(anchor.get("href", ""))
                title = " ".join(anchor.stripped_strings).strip()
                if title and "/t/" in href:
                    return title
        except Exception:
            return ""
        return ""

    @staticmethod
    def _live_id(source_id: str, review_id: str) -> str:
        digest = hashlib.sha256(f"weread-live|{source_id}|{review_id}".encode("utf-8")).hexdigest()[:16]
        return f"discovery-{digest}"

    def __call__(self, now) -> list[Any]:
        if not self.client.store.cookie():
            return []
        from reading_discovery import DiscoveredArticle
        from reading_exact import titles_match
        found: list[Any] = []
        with self._lock:
            state = self._load()
            accounts_state = state.setdefault("accounts", {})
            changed = False
            for source_id, source_name, account, index_url in self.accounts:
                saved = accounts_state.get(source_id) if isinstance(accounts_state.get(source_id), dict) else {}
                book_id = str(saved.get("book_id", ""))
                if not book_id:
                    book = self.client.find_mp_book(account)
                    if not book:
                        continue
                    book_id = str(book.get("bookId", ""))
                    if not book_id:
                        continue
                cover = self.client.latest_cover(book_id)
                review_id = str(cover.get("reviewId", "")).strip()
                title = str(cover.get("title", "")).strip()
                if not review_id or not title:
                    continue
                previous = str(saved.get("review_id", "")).strip()
                emit = bool(previous and previous != review_id)
                if not previous:
                    baseline_title = self._latest_index_title(index_url)
                    emit = bool(baseline_title and not titles_match(baseline_title, title))
                accounts_state[source_id] = {
                    "book_id": book_id, "review_id": review_id, "title": title, "checked_at": now.isoformat(),
                }
                changed = True
                if emit:
                    found.append(DiscoveredArticle(
                        id=self._live_id(source_id, review_id), source_id=source_id, source=source_name,
                        title=title, detail_url=mp_link_from_review(review_id, book_id),
                        published_at=now.isoformat(), discovered_at=now.isoformat(),
                    ))
            if changed:
                self._save(state)
        return found


class WeReadQrLogin:
    def __init__(self, store: WeReadAuthStore | None = None) -> None:
        self.store = store or WeReadAuthStore()
        self._lock = RLock()
        self._session: requests.Session | None = None
        self._uid = ""
        self._qr_data = ""
        self._state = "authorized" if self.store.cookie() else "idle"
        self._message = "已授权" if self.store.cookie() else "未授权"
        self._started_at = 0.0

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(_headers())
        return session

    @staticmethod
    def _qr_svg_data(confirm_url: str) -> str:
        import qrcode
        import qrcode.image.svg
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=7, border=3)
        qr.add_data(confirm_url)
        qr.make(fit=True)
        image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        out = BytesIO()
        image.save(out)
        encoded = base64.b64encode(out.getvalue()).decode("ascii")
        return "data:image/svg+xml;base64," + encoded

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": bool(self.store.cookie()),
                "state": self._state,
                "message": self._message,
                "qr": self._qr_data if self._state == "waiting" else "",
            }

    def start(self) -> dict[str, Any]:
        session = self._new_session()
        response = session.get(WEREAD_BASE + "/api/auth/getLoginUid", timeout=20)
        if response.status_code != 200:
            raise WeReadError(response.status_code, "微信读书二维码初始化失败")
        payload = response.json()
        uid = payload.get("uid") or (payload.get("data") or {}).get("uid")
        if not uid:
            raise WeReadError("missing_uid", "微信读书没有返回登录二维码")
        confirm_url = f"{WEREAD_BASE}/web/confirm?uid={uid}"
        with self._lock:
            self._session = session
            self._uid = str(uid)
            self._qr_data = self._qr_svg_data(confirm_url)
            self._state = "waiting"
            self._message = "请用微信扫码登录微信读书"
            self._started_at = time.time()
        Thread(target=self._poll_loop, daemon=True, name="reader-weread-login").start()
        return self.status()

    def _poll_loop(self) -> None:
        while time.time() - self._started_at < 300:
            try:
                result = self._check_login_once()
            except Exception as exc:
                result = {"succeed": False, "error": str(exc)}
            if result.get("succeed"):
                self._finish_login(result)
                return
            if result.get("need_otp"):
                with self._lock:
                    self._state = "error"; self._message = "微信读书要求验证码，当前请重新扫码"
                return
            time.sleep(2)
        with self._lock:
            self._state = "expired"; self._message = "二维码已过期，请重新获取"

    def _check_login_once(self) -> dict[str, Any]:
        session = self._session
        if session is None or not self._uid:
            return {"succeed": False}
        response = session.get(
            WEREAD_BASE + "/api/auth/getLoginInfo",
            params={"uid": self._uid, "otp": ""}, timeout=70,
        )
        if response.status_code != 200:
            return {"succeed": False, "error": f"HTTP {response.status_code}"}
        payload = response.json()
        inner = payload.get("data") or {}
        if not (payload.get("succeed") or inner.get("succeed")):
            logic = payload.get("logicCode") or inner.get("logicCode") or ""
            return {"succeed": False, "need_otp": logic == "NEED_OTP"}
        vid = (payload.get("webLoginVid") or payload.get("vid") or payload.get("userVid")
               or inner.get("webLoginVid") or inner.get("vid") or inner.get("userVid") or "")
        access = (payload.get("accessToken") or payload.get("access_token") or payload.get("token")
                  or inner.get("accessToken") or inner.get("access_token") or inner.get("token") or "")
        refresh = (payload.get("refreshToken") or payload.get("refresh_token")
                   or inner.get("refreshToken") or inner.get("refresh_token") or "")
        cookies = {c.name: c.value for c in session.cookies if c.name and c.value}
        if vid and not cookies.get("wr_vid"):
            cookies["wr_vid"] = str(vid)
        return {"succeed": True, "vid": str(vid or ""), "accessToken": str(access or ""),
                "refreshToken": str(refresh or ""), "cookies": cookies}

    @staticmethod
    def _cookie_candidates(result: dict[str, Any]) -> list[dict[str, str]]:
        base = dict(result.get("cookies") or {})
        vid = str(result.get("vid") or "")
        if vid and not base.get("wr_vid"):
            base["wr_vid"] = vid
        access = str(result.get("accessToken") or base.get("wr_skey") or "")
        refresh = str(result.get("refreshToken") or "")
        refresh_encoded = quote(refresh, safe="") if refresh else ""
        base_no_tokens = {k: v for k, v in base.items() if k not in {"wr_skey", "wr_rt"}}
        variants = [dict(base)]
        skeys = list(dict.fromkeys(x for x in (access, refresh, base.get("wr_skey", "")) if x))
        rts = list(dict.fromkeys(x for x in (refresh_encoded, refresh, base.get("wr_rt", "")) if x))
        for skey in skeys:
            variants.append(base_no_tokens | {"wr_skey": skey})
            for rt in rts:
                variants.append(base_no_tokens | {"wr_skey": skey, "wr_rt": rt})
        for rt in rts:
            variants.append(base_no_tokens | {"wr_rt": rt})
        seen: set[tuple[tuple[str, str], ...]] = set()
        out = []
        for row in variants:
            key = tuple(sorted((str(k), str(v)) for k, v in row.items() if v))
            if key and key not in seen:
                seen.add(key); out.append({k: str(v) for k, v in row.items() if v})
        return out

    @staticmethod
    def _verify(cookies: dict[str, str]) -> bool:
        try:
            response = requests.get(
                WEREAD_BASE + "/web/shelf/sync",
                params={"userVid": "", "synckey": 0}, headers=_headers(_cookie_string(cookies)), timeout=20,
            )
            if response.status_code != 200:
                return False
            payload = response.json()
            code = payload.get("errCode", payload.get("errcode", 0))
            return not code and any(key in payload for key in ("books", "bookCount", "synckey"))
        except Exception:
            return False

    @staticmethod
    def _renew(cookies: dict[str, str]) -> dict[str, str] | None:
        if not cookies.get("wr_rt"):
            return None
        session = requests.Session()
        session.headers.update(_headers())
        session.headers["Content-Type"] = "application/json"
        for key, value in cookies.items():
            session.cookies.set(key, value, domain="weread.qq.com", path="/")
        try:
            response = session.post(
                WEREAD_BASE + "/web/login/renewal",
                data=json.dumps({"rq": "%2Fweb%2Fbook%2Fread", "ql": True}, separators=(",", ":")),
                timeout=20,
            )
        except Exception:
            return None
        updated = dict(cookies)
        for cookie in session.cookies:
            if cookie.name in {"wr_skey", "wr_vid", "wr_rt"} and cookie.value:
                updated[cookie.name] = cookie.value
        return updated if response.status_code == 200 and updated.get("wr_skey") else None

    def _finish_login(self, result: dict[str, Any]) -> None:
        chosen = None
        for candidate in self._cookie_candidates(result):
            if self._verify(candidate):
                chosen = candidate
                break
            renewed = self._renew(candidate)
            if renewed and self._verify(renewed):
                chosen = renewed
                break
        if not chosen:
            with self._lock:
                self._state = "error"
                self._message = "扫码成功，但微信读书登录凭据验证失败，请重新扫码"
            return
        self.store.save(cookie=_cookie_string(chosen), vid=str(result.get("vid") or ""))
        with self._lock:
            self._state = "authorized"
            self._message = "微信读书已授权"
            self._qr_data = ""


_DEFAULT_LOGIN: WeReadQrLogin | None = None
_DEFAULT_LOCK = RLock()


def get_weread_login() -> WeReadQrLogin:
    global _DEFAULT_LOGIN
    with _DEFAULT_LOCK:
        if _DEFAULT_LOGIN is None:
            _DEFAULT_LOGIN = WeReadQrLogin()
        return _DEFAULT_LOGIN
