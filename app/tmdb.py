"""
TMDB 查询：把综艺的花絮素材对齐到 TMDB 特辑季（Season 0）的真实集号。

为什么需要它
------------
综艺的先导 / 纯享 / 加更 / 花絮在 TMDB 上属于 Season 0（特辑季），
集号是跨季累计的（例如「现在就出发」第4季第1期加更 = S00E138）。
Emby / Jellyfin 只有文件名里的 S00Exx 和 TMDB 集号一致时才能正确刮削，
所以花絮不能按「第几个文件」编号，必须按 TMDB 的实际集号。

网络说明
--------
本机到 api.themoviedb.org 的解析结果被污染/不可达，但 www.themoviedb.org
（CloudFront）可以正常连通，且同一 CloudFront 分布同时服务 api 域名。
因此这里先用正常 DNS 解析 www 的 IP，再以 api.themoviedb.org 作为
SNI / Host 直连，从而拿到 API 数据。
"""

import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse

# 结果缓存时间（秒）：剧集信息不会频繁变化
_TTL = 3600
_cache = {}
_lock = threading.Lock()
_ip_cache = {"ip": "", "at": 0.0}

# 花絮类型词：用于和 TMDB 特辑标题做匹配
_TAG_WORDS = (
    "先导", "纯享", "花絮", "加更", "预告", "彩蛋", "番外",
    "会员版", "特辑", "回顾", "未播", "幕后", "路透",
)


def _resolve_ip():
    """解析 www.themoviedb.org 的 IP（同一 CloudFront 也服务 api 域名）"""
    with _lock:
        if _ip_cache["ip"] and time.time() - _ip_cache["at"] < 600:
            return _ip_cache["ip"]
    ip = ""
    for host in ("www.themoviedb.org", "image.tmdb.org"):
        try:
            ip = socket.gethostbyname(host) or ""
            if ip:
                break
        except Exception:  # noqa: BLE001 - 换下一个
            continue
    with _lock:
        _ip_cache["ip"] = ip
        _ip_cache["at"] = time.time()
    return ip


def _request(api_key, path, params=None, timeout=15):
    """以固定 IP + api 域名 SNI 的方式请求 TMDB"""
    ip = _resolve_ip()
    if not ip:
        raise RuntimeError("无法解析 TMDB 地址")
    query = {"api_key": api_key, "language": "zh-CN"}
    if params:
        query.update(params)
    qs = "&".join(
        "%s=%s" % (k, urllib.parse.quote(str(v))) for k, v in query.items()
    )
    ctx = ssl.create_default_context()
    sock = socket.create_connection((ip, 443), timeout=timeout)
    try:
        tls = ctx.wrap_socket(sock, server_hostname="api.themoviedb.org")
        conn = http.client.HTTPSConnection("api.themoviedb.org", 443, timeout=timeout)
        conn.sock = tls
        conn.request(
            "GET",
            "%s?%s" % (path, qs),
            headers={"Host": "api.themoviedb.org", "User-Agent": "quark-auto-save"},
        )
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError("TMDB HTTP %s" % resp.status)
        return json.loads(body.decode("utf-8"))
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


def _cached(key, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def search_series(api_key, name):
    """按剧名搜 TMDB 剧集，返回 [{id, name, first_air_date}]"""

    def go():
        data = _request(api_key, "/3/search/tv", {"query": name})
        return [
            {
                "id": r.get("id"),
                "name": r.get("name") or r.get("original_name") or "",
                "first_air_date": r.get("first_air_date") or "",
            }
            for r in (data.get("results") or [])
        ]

    return _cached(("search", name), go)


def season_episodes(api_key, tv_id, season=0):
    """取某一季的分集列表（默认特辑季 Season 0）"""

    def go():
        data = _request(api_key, "/3/tv/%s/season/%s" % (tv_id, season))
        return [
            {
                "number": e.get("episode_number"),
                "name": e.get("name") or "",
                "air_date": e.get("air_date") or "",
            }
            for e in (data.get("episodes") or [])
        ]

    return _cached(("season", tv_id, season), go)


def name_date(name):
    """从文件名里取日期，返回 YYYY-MM-DD（取不到返回空串）

    兼容 20261012 / 2026.10.13 / 2026-10-1 这几种写法。
    """
    text = name or ""
    m = re.search(r"(20\d{2})[.\-_]?(\d{1,2})[.\-_]?(\d{1,2})", text)
    if not m:
        return ""
    year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return "%s-%02d-%02d" % (year, month, day)


def name_period(name):
    """从文件名里取「第N期」的期号"""
    m = re.search(r"第\s*(\d{1,4})\s*期", name or "")
    return int(m.group(1)) if m else None


def name_part(name):
    """从文件名里取 上/中/下"""
    text = name or ""
    m = re.search(r"期\s*([上中下])", text) or re.search(
        r"([上中下])\s*(?:[.\-_ ]|$)", text
    )
    return m.group(1) if m else ""


def name_tags(name):
    """从文件名里取花絮类型词"""
    low = (name or "").lower()
    return {t for t in _TAG_WORDS if t in low}


def _bigrams(text):
    """把文本切成二元组，用于粗略判断片名相似度（中文无需分词）"""
    t = re.sub(r"[\s.\-_:：·（）()【】\[\]，,！!？?、]", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _score(episode, filename, period, part, tags, season=None):
    """给特辑候选打分：标题里命中的信息越多越像"""
    ename = episode.get("name") or ""
    score = 0
    if season:
        if ("第%d季" % season) in ename:
            score += 4
        else:
            m = re.search(r"第\s*(\d{1,2})\s*季", ename)
            if m and int(m.group(1)) != season:
                score -= 4
    if period is not None:
        ep = name_period(ename)
        if ep is not None:
            score += 3 if ep == period else -3
    e_tags = name_tags(ename)
    if tags:
        score += 3 * len(tags & e_tags)
        score -= 2 * len(e_tags - tags)      # 标题里有、文件名里没有的类型词
    e_part = name_part(ename)
    if part and e_part:
        score += 2 if part == e_part else -2
    # 片名相似度：同一天往往有多条特辑（如两个不同的「纯享」片段），
    # 只能靠标题里的具体内容区分
    fn_grams, ep_grams = _bigrams(filename), _bigrams(ename)
    if fn_grams and ep_grams:
        score += min(len(fn_grams & ep_grams), 8)
    # 标题里带的期号/上下集和文件名冲突时，基本可以排除
    return score


def match_special(episodes, filename, season=None):
    """
    在特辑季里找文件名对应的那一集。

    优先按「文件名里的日期」筛候选，再按期号 / 上下集 / 类型词打分；
    没有日期时退化为全量打分。返回 {number, name, air_date, by} 或 None。
    """
    if not episodes:
        return None
    period = name_period(filename)
    part = name_part(filename)
    tags = name_tags(filename)
    date = name_date(filename)

    candidates = episodes
    by = "关键词"
    if date:
        same = [e for e in episodes if (e.get("air_date") or "") == date]
        if not same:
            # 文件名带日期、TMDB 当天却没有对应特辑（如回顾特辑），
            # 说明 TMDB 没收录，交给顺序号，绝不硬套一个错误的集号
            return None
        candidates = same
        by = "日期 " + date

    best, best_score = None, -10 ** 9
    for e in candidates:
        sc = _score(e, filename, period, part, tags, season)
        if sc > best_score:
            best, best_score = e, sc

    # 分数太低说明对不上（例如回顾特辑在 TMDB 上没有对应条目）
    if best is None or best_score <= 0:
        return None
    out = dict(best)
    out["by"] = by
    return out


def resolve_show(api_key, taskname):
    """按剧名找到 TMDB 剧集，返回剧集 id（找不到返回 None）"""
    try:
        shows = search_series(api_key, taskname)
    except Exception:  # noqa: BLE001 - 网络问题不影响入库
        return None
    if not shows:
        return None
    for s in shows:
        if (s.get("name") or "") == taskname:
            return s["id"]
    return shows[0]["id"]


def season_map_for_show(api_key, taskname, filenames, season):
    """
    正片的集号：按文件名里的日期 / 期号 / 上下集，匹配该季的真实集号
    （例如「第2期上」在 TMDB 是 S04E03）。返回 {文件名: 集号}。
    """
    if not api_key or not filenames or not season:
        return {}
    tv_id = resolve_show(api_key, taskname)
    if not tv_id:
        return {}
    try:
        episodes = season_episodes(api_key, tv_id, season)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for fn in filenames:
        hit = match_special(episodes, fn, None)
        if hit and hit.get("number"):
            out[fn] = int(hit["number"])
    return out


def specials_for_show(api_key, taskname, filenames, season=None):
    """
    一次性把一批花絮文件映射到 TMDB 特辑季集号。

    返回 {文件名: {number, name, air_date, by}}，失败时返回 {}。
    """
    if not api_key or not filenames:
        return {}
    tv_id = resolve_show(api_key, taskname)
    if not tv_id:
        return {}
    try:
        episodes = season_episodes(api_key, tv_id, 0)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for fn in filenames:
        hit = match_special(episodes, fn, season)
        if hit and hit.get("number"):
            out[fn] = hit
    return out


def seasons_for_show(api_key, name):
    """
    这部剧有哪些季（返回季号列表，按播出时间从早到晚）。

    库里还没有这部剧本季目录时，用它推断「现在更新到第几季」，
    好自动建出 Sxx 子目录。
    """
    if not api_key or not name:
        return []
    tv_id = resolve_show(api_key, name)   # 返回的是剧集 id（int）
    if not tv_id:
        return []

    def _fetch():
        data = _request(api_key, "/3/tv/%s" % tv_id)
        rows = []
        for season in data.get("seasons") or []:
            number = season.get("season_number")
            if not isinstance(number, int) or number < 1:
                continue
            if not (season.get("episode_count") or 0):
                continue
            rows.append((season.get("air_date") or "", number))
        rows.sort()
        return [n for _, n in rows]

    return _cached(("seasons", tv_id), _fetch)


def season_for_date(api_key, name, date):
    """
    某个播出日期属于第几季（返回季号）。

    分享根目录常混着几期最新的节目，它们不在任何季目录里，
    靠文件名里的日期问 TMDB 就知道该进 S09 还是 S10。
    """
    if not api_key or not name or not date:
        return None
    tv_id = resolve_show(api_key, name)
    if not tv_id:
        return None
    for season in reversed(seasons_for_show(api_key, name)):
        try:
            episodes = season_episodes(api_key, tv_id, season)
        except Exception:  # noqa: BLE001
            continue
        dates = [e.get("air_date") for e in episodes if e.get("air_date")]
        if dates and min(dates) <= date <= max(dates):
            return season
    return None


def episode_for_date(api_key, name, season, date):
    """某一季里某个播出日期对应第几集"""
    if not api_key or not name or not season or not date:
        return None
    tv_id = resolve_show(api_key, name)
    if not tv_id:
        return None
    try:
        episodes = season_episodes(api_key, tv_id, season)
    except Exception:  # noqa: BLE001
        return None
    for ep in episodes:
        if (ep.get("air_date") or "") == date:
            number = ep.get("number")      # season_episodes 用的是 number
            return number if isinstance(number, int) else None
    return None


def alternative_titles(api_key, name):
    """
    剧名/片名的别名（含中英文），用来替代模型做「中英互译」。

    搜「The Knockout」能带出「狂飙」，搜「绝命毒师」能带出「Breaking Bad」。
    """
    if not api_key or not name:
        return []
    tv_id = resolve_show(api_key, name)
    if not tv_id:
        return []
    # TMDB 偶发 5xx/超时；一次失败就返回空会让英文标题的分享全被过滤掉，
    # 所以重试一次再放弃。
    for attempt in range(2):
        try:
            data = _request(api_key, "/3/tv/%s/alternative_titles" % tv_id)
            return [
                {"title": (t.get("title") or "").strip(), "country": (t.get("iso_3166_1") or "")}
                for t in (data.get("results") or [])
                if (t.get("title") or "").strip()
            ]
        except Exception:  # noqa: BLE001
            if attempt == 1:
                return []
    return []


# 优先采用的别名地区：英文（美/英）+ 中文（大陆/台/港/新）
PREFERRED_ALIAS_COUNTRIES = ("US", "GB", "CN", "TW", "HK", "SG")


def pick_aliases(items, limit=6):
    """
    从 TMDB 别名里挑出有用的：优先英文/中文，其余按原顺序补足。

    不筛的话会拿到阿尔巴尼亚语、西班牙语这类译名，
    拿去搜网盘纯属浪费一次 4 秒的查询。
    """
    preferred, others = [], []
    for item in items or []:
        title = (item.get("title") or "").strip()
        # 两个字符的别名（BB、GoT 之外的缩写）太松：
        # 「BB」会把「BB酱」这种无关短剧也匹配进来，检索也搜不出东西
        if len(title) < 3:
            continue
        if item.get("country") in PREFERRED_ALIAS_COUNTRIES:
            preferred.append(title)
        else:
            others.append(title)
    out = []
    for title in preferred + others:
        if title not in out:
            out.append(title)
        if len(out) >= limit:
            break
    return out


def _norm_title(text):
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def media_kind(api_key, name):
    """
    判断是剧集还是电影，并带回类型（用来定「电视剧/电影/动漫/综艺/纪录片」）。

    剧集和电影都查一遍：同名的两边都可能有（「流浪地球」既有电影也有衍生剧），
    精确同名优先，其次保持剧集优先。
    返回 {"kind": "tv"/"movie", "genres": [int], "name": str}，查不到返回 None。
    """
    if not api_key or not name:
        return None
    target = _norm_title(name)

    tv = None
    tv_id = resolve_show(api_key, name)
    if tv_id:
        try:
            data = _request(api_key, "/3/tv/%s" % tv_id)
        except Exception:  # noqa: BLE001
            data = {}
        tv = {
            "kind": "tv",
            "genres": [g.get("id") for g in (data.get("genres") or []) if g.get("id")],
            "name": data.get("name") or "",
            # 原始名（通常是英文原名）：alternative_titles 不含它，
            # 但英文名分享（Game.of.Thrones）必须靠它才匹配得上
            "original_name": data.get("original_name") or "",
        }

    movie = None

    def go():
        data = _request(api_key, "/3/search/movie", {"query": name})
        return data.get("results") or []

    try:
        results = _cached(("search_movie", name), go)
    except Exception:  # noqa: BLE001
        results = []
    if results:
        top = results[0]
        genres = []
        if top.get("id"):
            try:
                detail = _request(api_key, "/3/movie/%s" % top["id"])
                genres = [g.get("id") for g in (detail.get("genres") or []) if g.get("id")]
            except Exception:  # noqa: BLE001
                pass
        movie = {
            "kind": "movie",
            "genres": genres,
            "name": top.get("title") or "",
            "original_name": top.get("original_title") or "",
        }

    # 精确同名的优先
    if movie and _norm_title(movie["name"]) == target and not (
        tv and _norm_title(tv["name"]) == target
    ):
        return movie
    if tv:
        return tv
    return movie


def category_from_kind(info):
    """把 TMDB 的类型映射成媒体库分类"""
    if not info:
        return ""
    genres = set(info.get("genres") or [])
    if info.get("kind") == "movie":
        return "电影"
    if genres & {10764, 10767}:      # Reality / Talk → 综艺
        return "综艺"
    if 16 in genres:                 # Animation → 动漫
        return "动漫"
    if 99 in genres:                 # Documentary → 纪录片
        return "纪录片"
    return "电视剧"
