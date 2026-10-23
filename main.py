"""B 站策展插件：按需刷、按需收藏、按需评论。

设计原则：
- 没有任何定时任务，只有用户发命令才动作
- 收藏/评论都必须用户点名，不自动执行
- 所有写操作（收藏、评论）前都有确认环节
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderType

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

API_QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
API_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
API_RANKING = "https://api.bilibili.com/x/web-interface/ranking/v2"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYER = "https://api.bilibili.com/x/player/v2"
API_FAV_FOLDERS = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
API_FAV_ADD_FOLDER = "https://api.bilibili.com/x/v3/fav/folder/add"
API_FAV_DEAL = "https://api.bilibili.com/x/v3/fav/resource/deal"

MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

TRIGGER_SCAN = ("刷b站", "刷B站", "刷刷b站", "刷一下b站")
TRIGGER_LOGIN = ("b站登录", "B站登录", "登录b站", "登录B站", "bili login")
TRIGGER_STATUS = ("b站状态", "B站状态")
TRIGGER_REPORT = ("今天看了什么", "看了什么", "今天刷了什么")
TRIGGER_FAV = re.compile(r"^收藏\s*([0-9,，\s]+)$")
TRIGGER_CONFIRM = ("建", "建立", "创建")

# 标题里带这些词的，默认判为"面向新手/凑数"，除非用户明确要教程
NEG_WORDS = ("零基础", "入门", "速成", "全套", "合集", "保姆", "从0到1",
             "小白", "教程", "不挂科", "期末", "自学")
WANT_TUTORIAL = ("教程", "入门", "学习", "零基础")



class BiliError(Exception):
    pass


class Bili:
    def __init__(self, cookie: dict | None = None):
        self.cookie = cookie or {}
        self._wbi: tuple[str, str, float] | None = None

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookie.items())

    async def _get(self, url: str, params: dict | None = None) -> dict:
        headers = dict(HEADERS)
        if self.cookie:
            headers["Cookie"] = self._cookie_header()
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
        if data.get("code") != 0:
            raise BiliError(f"{data.get('code')} {data.get('message')}")
        return data.get("data") or {}

    async def _post(self, url: str, data: dict) -> dict:
        headers = dict(HEADERS)
        if self.cookie:
            headers["Cookie"] = self._cookie_header()
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.post(url, data=data, timeout=aiohttp.ClientTimeout(total=20)) as r:
                out = await r.json(content_type=None)
        if out.get("code") != 0:
            raise BiliError(f"{out.get('code')} {out.get('message')}")
        return out.get("data") or {}

    # ---------- 登录 ----------
    async def qr_start(self) -> tuple[str, str]:
        """创建独立会话请求二维码，保留会话以便后续轮询共享 Cookie。"""
        self._qr_session = aiohttp.ClientSession(headers=HEADERS, cookie_jar=aiohttp.CookieJar(unsafe=True))
        async with self._qr_session.get(API_QR_GEN) as r:
            body = await r.json(content_type=None)
        data = body.get("data") or {}
        if not data.get("url"):
            raise BiliError(f"二维码生成失败: {body.get('code')} {body.get('message')}")
        return data["url"], data["qrcode_key"]

    async def qr_poll(self, key: str) -> tuple[int, dict, dict]:
        """返回 (状态码, data, cookies)。cookie 同时从 URL 参数和 Set-Cookie 收集。"""
        session = getattr(self, "_qr_session", None)
        own = session is None
        if own:
            session = aiohttp.ClientSession(headers=HEADERS, cookie_jar=aiohttp.CookieJar(unsafe=True))
        try:
            async with session.get(
                API_QR_POLL, params={"qrcode_key": key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                body = await r.json(content_type=None)
            data = body.get("data") or {}
            code = data.get("code", -1)
            cookies = {}
            try:
                for c in session.cookie_jar:
                    cookies[c.key] = c.value
            except Exception:
                pass
            return code, data, cookies
        finally:
            if own:
                await session.close()

    async def qr_finish(self):
        s = getattr(self, "_qr_session", None)
        if s is not None:
            try:
                await s.close()
            except Exception:
                pass
            self._qr_session = None

    async def nav(self) -> dict:
        return await self._get(API_NAV)

    # ---------- 内容 ----------
    async def popular(self, ps: int = 20) -> list[dict]:
        data = await self._get(API_POPULAR, {"ps": ps, "pn": 1})
        return data.get("list") or []

    async def ranking(self, rid: int = 0) -> list[dict]:
        data = await self._get(API_RANKING, {"rid": rid, "type": "all"})
        return data.get("list") or []

    async def tech_ranking(self, rid: int = 188) -> list[dict]:
        """科技分区排行，默认 rid=188（科技）。"""
        return await self.ranking(rid)

    # ---------- wbi 签名 ----------
    async def _wbi_keys(self) -> tuple[str, str]:
        if self._wbi and time.time() - self._wbi[2] < 3600:
            return self._wbi[0], self._wbi[1]
        data = await self._get(API_NAV)
        wbi = data.get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError("拿不到 wbi key")
        self._wbi = (img, sub, time.time())
        return img, sub

    async def signed_get(self, url: str, params: dict) -> dict:
        from hashlib import md5
        from urllib.parse import urlencode

        img, sub = await self._wbi_keys()
        mixin = "".join((img + sub)[i] for i in MIXIN_TAB)[:32]
        p = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
        p["wts"] = int(time.time())
        query = urlencode(sorted(p.items()))
        p["w_rid"] = md5((query + mixin).encode()).hexdigest()
        return await self._get(url, p)

    async def search_video(self, keyword: str, limit: int = 20) -> list[dict]:
        data = await self.signed_get(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            {"search_type": "video", "keyword": keyword, "page": 1},
        )
        out = []
        for it in (data.get("result") or [])[:limit]:
            title = re.sub(r"<[^>]+>", "", it.get("title") or "")
            out.append(
                {
                    "bvid": it.get("bvid"),
                    "title": title,
                    "owner": {"name": it.get("author") or ""},
                    "aid": it.get("aid"),
                    "duration": it.get("duration"),
                }
            )
        return out

    async def view(self, bvid: str) -> dict:
        return await self._get(API_VIEW, {"bvid": bvid})

    async def subtitle_text(self, bvid: str, cid: int, max_chars: int = 1800) -> str:
        try:
            data = await self._get(API_PLAYER, {"bvid": bvid, "cid": cid})
            subs = ((data.get("subtitle") or {}).get("subtitles")) or []
            if not subs:
                return ""
            url = subs[0].get("subtitle_url") or ""
            if url.startswith("//"):
                url = "https:" + url
            if not url:
                return ""
            async with aiohttp.ClientSession(headers=HEADERS) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    js = await r.json(content_type=None)
            parts = [it.get("content", "") for it in (js.get("body") or [])]
            return " ".join(parts)[:max_chars]
        except Exception as e:
            logger.debug(f"[bili] subtitle failed for {bvid}: {e}")
            return ""

    # ---------- 收藏 ----------
    async def fav_folders(self, mid: int) -> list[dict]:
        data = await self._get(API_FAV_FOLDERS, {"up_mid": mid})
        return data.get("list") or []

    async def fav_create_folder(self, name: str) -> int:
        csrf = self.cookie.get("bili_jct", "")
        data = await self._post(
            API_FAV_ADD_FOLDER,
            {"title": name, "privacy": 1, "csrf": csrf},
        )
        return int(data.get("id"))

    async def fav_add(self, aid: int, folder_id: int) -> None:
        csrf = self.cookie.get("bili_jct", "")
        await self._post(
            API_FAV_DEAL,
            {"rid": aid, "type": 2, "add_media_ids": folder_id, "csrf": csrf},
        )


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS items(
                bvid TEXT PRIMARY KEY,
                title TEXT, up TEXT, category TEXT, score INTEGER,
                reason TEXT, aid INTEGER, folder TEXT, faved INTEGER DEFAULT 0,
                created_at INTEGER
            )"""
        )
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()

    def meta_get(self, k: str, default=None):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def meta_set(self, k: str, v):
        self.conn.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v, ensure_ascii=False)),
        )
        self.conn.commit()

    def add_item(self, **kw):
        kw.setdefault("created_at", int(time.time()))
        cols = ",".join(kw)
        marks = ",".join("?" * len(kw))
        self.conn.execute(
            f"INSERT OR REPLACE INTO items({cols}) VALUES({marks})", tuple(kw.values())
        )
        self.conn.commit()

    def today(self) -> list[dict]:
        start = int(time.time()) - 86400
        cur = self.conn.execute(
            "SELECT bvid,title,up,category,score,reason,faved FROM items "
            "WHERE created_at>=? ORDER BY score DESC",
            (start,),
        )
        keys = ["bvid", "title", "up", "category", "score", "reason", "faved"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def mark_faved(self, bvid: str, folder: str):
        self.conn.execute("UPDATE items SET faved=1, folder=? WHERE bvid=?", (folder, bvid))
        self.conn.commit()


@register("bili_curator", "dsh", "B 站策展：按需刷、按需收藏、按需评论", "1.0.0")
class BiliCurator(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        self.cfg = cfg
        base = Path("/AstrBot/data/plugin_data/bili_curator")
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.cookie_file = base / "cookie.json"
        self.store = Store(base / "bili.db")
        self.cookie = self._load_cookie()
        self.bili = Bili(self.cookie)
        # 会话级状态
        self._last_scan: dict[str, list[dict]] = {}
        self._pending_folder: dict[str, dict] = {}

    # ---------- 基础 ----------
    def _load_cookie(self) -> dict:
        if self.cookie_file.exists():
            try:
                return json.loads(self.cookie_file.read_text("utf-8"))
            except Exception:
                return {}
        return {}

    def _save_cookie(self, cookie: dict):
        self.cookie_file.write_text(json.dumps(cookie), "utf-8")
        try:
            os.chmod(self.cookie_file, 0o600)
        except Exception:
            pass
        self.cookie = cookie
        self.bili = Bili(cookie)

    async def _reply(self, event: AstrMessageEvent, text: str):
        await event.send(event.chain_result([Plain(text)]))

    # ---------- 让模型知道自己的能力 ----------
    @filter.on_llm_request()
    async def inject_capability(self, event: AstrMessageEvent, req):
        if self.cookie:
            try:
                nav = await self.bili.nav()
                who = f"{nav.get('uname')}（UID {nav.get('mid')}）"
                logged = True
            except Exception:
                who = "账号"
                logged = False
        else:
            who, logged = "未登录", False

        state = f"已登录 B 站账号：{who}" if logged else "还没有登录 B 站"
        recent = self.store.today()
        seen = ""
        if recent:
            items = "；".join(
                f"《{r['title']}》（{r['category']}，{r['score']}分）" for r in recent[:5]
            )
            seen = (
                "\n你最近自己刷过 B 站，看过这些："
                f"{items}。\n"
                "这些是你自己的浏览经历，可以自然提起，也可以在被问“今天看了什么”时回答；"
                "不要说自己没看过。"
            )
        note = (
            "\n\n[关于 B 站能力，这是事实，不要否认]\n"
            f"你运行在用户的服务器上，装有 B 站插件。当前状态：{state}。\n"
            "你可以执行这些操作（由插件完成，不是你在网页上搜）：\n"
            "- 用户发「刷B站 关键词」：你去 B 站搜索/榜单取视频，看字幕，按用户兴趣打分，回报简报\n"
            "- 用户发「收藏 1」：把第 1 条收藏进用户 B 站账号的收藏夹\n"
            "- 用户发「今天看了什么」：读本地记录回答\n"
            "当用户问你有没有登录、能不能刷 B 站时，按上面的真实状态回答；"
            "不要说“我没有登录”“我只能搜网页”这类话。\n"
            "如果用户想刷但还没登录，提醒他发「B站登录」扫码。"
            + seen
        )
        try:
            req.system_prompt = (req.system_prompt or "") + note
        except Exception as e:
            logger.warning(f"[bili] inject failed: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        low = text.lower()
        norm = re.sub(r"\s+", "", low)
        logger.debug(f"[bili] on_message: {text[:40]}")

        # 登录 / 状态（放在刷之前，避免被状态判断吞掉）
        if norm in ("b站登录", "登录b站", "bililogin", "b站扫码") or norm.endswith("b站登录"):
            event.stop_event()
            await self.cmd_login(event)
            return
        if ("登录" in norm and "b站" in norm) or norm in ("b站状态", "b站账号"):
            event.stop_event()
            await self.cmd_status(event)
            return

        # 刷：允许「刷b站」「刷一下b站」「b站刷」「看看b站」
        if any(k in norm for k in ("刷b站", "刷一下b站", "b站刷", "看看b站", "b站看看")):
            event.stop_event()
            await self.cmd_scan(event, text)
            return

        if any(t in text for t in TRIGGER_REPORT):
            event.stop_event()
            await self.cmd_report(event)
            return
        m = TRIGGER_FAV.match(text)
        if m:
            event.stop_event()
            await self.cmd_fav(event, m.group(1))
            return
        if norm in [c.lower() for c in TRIGGER_CONFIRM]:
            if event.unified_msg_origin in self._pending_folder:
                event.stop_event()
                await self.cmd_confirm_folder(event)
            return
            return

    # ---------- 登录 ----------
    async def cmd_login(self, event: AstrMessageEvent):
        if self.cookie:
            try:
                nav = await self.bili.nav()
                if nav.get("isLogin"):
                    await self._reply(event, f"已经登录了：{nav.get('uname')}（UID {nav.get('mid')}）")
                    return
            except Exception:
                pass
        try:
            url, key = await self.bili.qr_start()
        except Exception as e:
            await self._reply(event, f"生成二维码失败：{e}")
            return
        import qrcode  # 延迟导入，避免影响插件加载

        img = qrcode.make(url)
        path = self.base / "qr.png"
        img.save(path)
        await event.send(event.chain_result([Plain("用 B 站 App 扫这个码，180 秒内有效：")]))
        await event.send(event.image_result(str(path)))
        asyncio.create_task(self._poll_qr(event, key))

    async def _poll_qr(self, event: AstrMessageEvent, key: str):
        for _ in range(60):
            await asyncio.sleep(3)
            try:
                code, data, jar_cookies = await self.bili.qr_poll(key)
            except Exception as e:
                logger.debug(f"[bili] poll err {e}")
                continue
            if code == 0:
                logger.info(
                    f"[bili] qr ok. data_keys={list(data)} "
                    f"jar_cookies={list(jar_cookies)} url_len={len(data.get('url') or '')}"
                )
                cookie = self._parse_cookie(data.get("url", ""))
                for k, v in (jar_cookies or {}).items():
                    if k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "b_nut"):
                        cookie.setdefault(k, v)
                await self.bili.qr_finish()
                if not cookie.get("SESSDATA"):
                    await self._reply(
                        event,
                        "登录成功但没拿到 SESSDATA。\n"
                        f"返回字段：{list(data)}\nCookie 头：{list(jar_cookies or {})}",
                    )
                    return
                self._save_cookie(cookie)
                try:
                    nav = await self.bili.nav()
                    who = f"{nav.get('uname')}（UID {nav.get('mid')}）"
                except Exception:
                    who = "未知账号"
                await self._reply(event, f"登录成功 ✅\n当前账号：{who}\nCookie 已保存。")
                return
            if code == 86038:
                await self.bili.qr_finish()
                await self._reply(event, "二维码过期了，重新发一次「B站登录」。")
                return

    @staticmethod
    def _parse_cookie(url: str) -> dict:
        from urllib.parse import urlparse, parse_qs

        q = parse_qs(urlparse(url).query)
        keys = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "b_nut"]
        return {k: q[k][0] for k in keys if k in q}

    # ---------- 状态 ----------
    async def cmd_status(self, event: AstrMessageEvent):
        if not self.cookie:
            await self._reply(event, "还没登录。发「B站登录」扫码。")
            return
        try:
            nav = await self.bili.nav()
            await self._reply(
                event,
                f"账号：{nav.get('uname')}（UID {nav.get('mid')}）\n"
                f"登录状态：{'有效' if nav.get('isLogin') else '已失效'}\n"
                f"VIP：{'是' if nav.get('vipStatus') else '否'}",
            )
        except Exception as e:
            await self._reply(event, f"查询失败：{e}（可能 Cookie 过期，重新扫码）")

    # ---------- 刷 ----------
    async def cmd_scan(self, event: AstrMessageEvent, text: str):
        if not self.cookie:
            await self._reply(event, "先发「B站登录」扫码，收藏和字幕都需要登录。")
            return

        terms = [t for t in re.split(r"[\s,，]+", text) if t and not t.lower().startswith("刷")]
        limit = int(self.cfg.get("fetch_limit", 10))
        await self._reply(event, f"开始刷，候选 {limit} 条，稍等…")

        try:
            if terms:
                pool = []
                for t in terms[:3]:
                    pool += await self.bili.search_video(t, limit)
                use_search = True
            else:
                pool = (await self.bili.tech_ranking(188)) + (await self.bili.popular(20))
                use_search = False
        except Exception as e:
            await self._reply(event, f"拉列表失败：{e}")
            return

        seen, candidates = set(), []
        want_tut = any(w in (text or "") for w in WANT_TUTORIAL)
        for it in pool:
            bvid = it.get("bvid")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            title = it.get("title") or ""
            # 搜索已经按关键词命中，不再二次过滤
            if terms and not use_search and not any(t.lower() in title.lower() for t in terms):
                continue
            if not want_tut and any(w in title for w in NEG_WORDS):
                continue
            candidates.append(it)
            if len(candidates) >= limit:
                break

        # 过滤太狠导致没剩几条时，放宽一次
        if len(candidates) < 3 and not want_tut:
            for it in pool:
                bvid = it.get("bvid")
                if bvid and bvid not in seen:
                    seen.add(bvid)
                    candidates.append(it)
                if len(candidates) >= 3:
                    break

        if not candidates:
            await self._reply(event, "这一轮没找到匹配的视频，换个关键词试试。")
            return

        provider = self.context.get_using_provider(event.unified_msg_origin)
        if not provider:
            await self._reply(event, "没有可用的对话模型。")
            return

        results = []
        for it in candidates:
            bvid = it["bvid"]
            try:
                info = await self.bili.view(bvid)
                cid = (info.get("cid") or (info.get("pages") or [{}])[0].get("cid"))
                sub = await self.bili.subtitle_text(bvid, cid) if cid else ""
                stat = it.get("stat") or {}
                play = it.get("play") or stat.get("view") or 0
                favs = it.get("favorites") or stat.get("favorite") or 0
                meta = await self._score(
                    provider, it.get("title", ""), info.get("desc", ""), sub, terms, play, favs
                )
                item = {
                    "bvid": bvid,
                    "aid": info.get("aid"),
                    "title": it.get("title", ""),
                    "up": ((info.get("owner") or {}).get("name")) or it.get("owner", {}).get("name", ""),
                    "category": meta.get("category", "其他"),
                    "score": int(meta.get("score", 0)),
                    "reason": meta.get("reason", ""),
                    "has_sub": bool(sub),
                }
                results.append(item)
                self.store.add_item(**{k: item[k] for k in
                                       ("bvid", "title", "up", "category", "score", "reason", "aid")})
            except Exception as e:
                logger.warning(f"[bili] scan item {bvid} failed: {e}")

        if not results:
            await self._reply(event, "这一轮全部失败了，可能是接口风控，过会儿再试。")
            return

        results.sort(key=lambda x: x["score"], reverse=True)
        top = results[: int(self.cfg.get("max_items", 5))]
        self._last_scan[event.unified_msg_origin] = top

        lines = [f"刷完 {len(results)} 条，按评分排序：", ""]
        for i, r in enumerate(top, 1):
            lines.append(
                f"{i}. {r['title']}\n"
                f"   {r['category']} · {r['score']}/10 · {r['up']}\n"
                f"   {r['reason']}\n"
                f"   https://b23.tv/{r['bvid']}"
            )
        lines.append("")
        lines.append("要收藏哪条？回复「收藏 1」或「收藏 1,2」。")
        list_text = "\n".join(lines)

        # 让模型用自己的口吻说一句"我刚刷了"，而不是模板腔
        lead = ""
        try:
            digest = "\n".join(
                f"{i}. {r['title']}（{r['category']}，{r['score']}分，{r['reason']}）"
                for i, r in enumerate(top, 1)
            )
            resp = await provider.text_chat(
                prompt=(
                    "你刚刚自己刷完了一轮 B 站，结果如下：\n"
                    f"{digest}\n\n"
                    "用你自己的口吻、第一人称，写 1–2 句你的感受或吐槽，"
                    "像刚刷完手机随口跟人说的那样。不要罗列，不要 Markdown，"
                    "不要重复标题，不超过 60 字。"
                ),
                system_prompt=(
                    "你是用户 QQ 里的 AI 朋友，有自己的人设和语气。"
                    "这件事是你主动去做的，不是执行命令，用自然口语，别用客服腔。"
                ),
            )
            lead = (resp.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"[bili] narrate failed: {e}")

        await self._reply(event, (lead + "\n\n" if lead else "") + list_text)

    async def _score(
        self, provider, title: str, desc: str, sub: str,
        terms: list[str], play: int = 0, favs: int = 0,
    ) -> dict:
        interests = self.cfg.get("preferences") or "C++、逆向、Linux、硬件、AI 工程"
        prompt = (
            f"我的兴趣：{interests}\n"
            f"本轮特别关注：{'、'.join(terms) if terms else '科技区综合'}\n\n"
            f"视频标题：{title}\n"
            f"播放量：{play}　收藏量：{favs}\n"
            f"简介：{(desc or '')[:500]}\n"
            f"字幕节选：{(sub or '无字幕')[:1200]}\n\n"
            "请判断这个视频对我是否有用。只输出 JSON，不要解释：\n"
            '{"score": 0-10 的整数, "category": "分类", "reason": "20 字以内理由"}\n'
            "category 只能取以下之一：C++、逆向、Linux、硬件、AI、数码、编程、其他。\n"
            "评分标准：8-10 是直接有用（技术深度/可实操），5-7 是相关可看，"
            "0-4 是无关或纯娱乐。\n"
            "注意：面向新手的入门课程、速成课、合集搬运，最多给 4 分。"
        )
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你是内容筛选助手，只输出 JSON，不要解释。",
            )
            text = (resp.completion_text or "").strip()
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                return json.loads(m.group(0))
        except Exception as e:
            logger.warning(f"[bili] score failed: {e}")
        return {"score": 0, "category": "其他", "reason": "评估失败"}

    # ---------- 收藏 ----------
    async def cmd_fav(self, event: AstrMessageEvent, nums: str):
        items = self._last_scan.get(event.unified_msg_origin) or []
        if not items:
            await self._reply(event, "还没有刷过，先发「刷B站」。")
            return
        indexes = []
        for part in re.split(r"[,，\s]+", nums.strip()):
            if part.isdigit():
                indexes.append(int(part))
        picked = [items[i - 1] for i in indexes if 1 <= i <= len(items)]
        if not picked:
            await self._reply(event, "编号不对，看一下上一条消息里的列表。")
            return
        if len(picked) > int(self.cfg.get("max_fav", 3)):
            await self._reply(event, f"一次最多收藏 {self.cfg.get('max_fav', 3)} 条。")
            return

        for it in picked:
            ok = await self._fav_one(event, it)
            if ok is False:
                return

    async def _fav_one(self, event: AstrMessageEvent, it: dict) -> bool | None:
        folder_name = f"AI-{it['category']}"
        try:
            nav = await self.bili.nav()
            mid = nav.get("mid")
            folders = await self.bili.fav_folders(mid)
        except Exception as e:
            await self._reply(event, f"查收藏夹失败：{e}")
            return False

        target = next((f for f in folders if f.get("title") == folder_name), None)
        if not target:
            if len(folders) >= int(self.cfg.get("max_folders", 5)):
                await self._reply(event, f"收藏夹已有 {len(folders)} 个，达到上限，先手动整理一下。")
                return False
            self._pending_folder[event.unified_msg_origin] = {"item": it, "name": folder_name}
            await self._reply(
                event,
                f"没有「{folder_name}」这个收藏夹，要新建一个吗？\n回复「建」就创建并收藏这条。",
            )
            return False

        try:
            await self.bili.fav_add(int(it["aid"]), int(target["id"]))
            self.store.mark_faved(it["bvid"], folder_name)
            await self._reply(event, f"已收藏到「{folder_name}」：{it['title']}")
        except Exception as e:
            await self._reply(event, f"收藏失败：{e}")
            return False
        await asyncio.sleep(2)
        return True

    async def cmd_confirm_folder(self, event: AstrMessageEvent):
        pending = self._pending_folder.pop(event.unified_msg_origin, None)
        if not pending:
            return
        it, name = pending["item"], pending["name"]
        try:
            fid = await self.bili.fav_create_folder(name)
            await asyncio.sleep(1)
            await self.bili.fav_add(int(it["aid"]), fid)
            self.store.mark_faved(it["bvid"], name)
            await self._reply(event, f"已创建「{name}」并收藏：{it['title']}")
        except Exception as e:
            await self._reply(event, f"创建或收藏失败：{e}")

    # ---------- 问答 ----------
    async def cmd_report(self, event: AstrMessageEvent):
        rows = self.store.today()
        if not rows:
            await self._reply(event, "最近 24 小时还没刷过 B 站。发「刷B站」试试。")
            return
        lines = [f"最近 24 小时看了 {len(rows)} 条：", ""]
        for i, r in enumerate(rows[:10], 1):
            flag = "⭐已收藏" if r["faved"] else ""
            lines.append(f"{i}. {r['title']}（{r['category']} {r['score']}分）{flag}")
            if r["reason"]:
                lines.append(f"   {r['reason']}")
        await self._reply(event, "\n".join(lines))
