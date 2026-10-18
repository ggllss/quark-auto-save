# -*- coding: utf-8 -*-
"""
AI 编排层（OpenAI 兼容接口）。

职责边界（重要）：
- 本模块只处理「不确定」的语义工作：自然语言意图解析、候选链接排序、命名规则推荐。
- 「哪些剧集可更新」属于确定性判断，由 MagicRename + 目标目录已有文件比对完成，
  不交给模型，避免幻觉导致漏转或误判。

所有对外函数都保证不抛异常：AI 未启用 / 网络失败 / 返回非法 JSON 时，
自动降级为本地启发式实现，功能仍可用。
"""

import json
import re

import requests

DEFAULT_TIMEOUT = 60

# 允许出现在替换模板里的变量（与 MagicRename.magic_variable 对齐）
ALLOWED_VARS = [
    "{TASKNAME}", "{SXX}", "{S}", "{E}", "{I}", "{EXT}",
    "{PART}", "{DATE}", "{YEAR}", "{CHINESE}", "{VER}",
]

# 季号识别
_SEASON_PATTERNS = [
    r"第\s*([0-9一二三四五六七八九十]+)\s*[季部]",
    r"(?:season|s)\s*([0-9]{1,2})\b",
    r"\bS([0-9]{1,2})\b",
]
_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


class AIError(Exception):
    """AI 调用相关错误"""


class AIClient:
    """OpenAI 兼容的 Chat Completions 客户端（DeepSeek / 硅基流动 / Ollama 等同理）。"""

    def __init__(self, config=None):
        cfg = config or {}
        self.enable = _as_bool(cfg.get("enable"))
        self.base_url = (cfg.get("base_url") or "").strip().rstrip("/")
        self.api_key = (cfg.get("api_key") or "").strip()
        self.model = (cfg.get("model") or "").strip()
        try:
            self.timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT

    @property
    def ready(self):
        """是否具备调用条件（api_key 允许为空，兼容本地 Ollama）"""
        return bool(self.enable and self.base_url and self.model)

    def chat(self, system, user, temperature=0.2):
        """发起一次对话，返回文本内容。失败抛 AIError。"""
        if not self.ready:
            raise AIError("AI 未启用或配置不完整（需要 enable / base_url / model）")

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        base_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }

        # 部分服务不支持 response_format，失败后去掉重试一次
        last_err = None
        for with_json_mode in (True, False):
            payload = dict(base_payload)
            if with_json_mode:
                payload["response_format"] = {"type": "json_object"}
            try:
                resp = requests.post(
                    url, json=payload, headers=headers, timeout=self.timeout
                )
                if resp.status_code >= 400 and with_json_mode:
                    last_err = AIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except AIError:
                raise
            except Exception as e:  # noqa: BLE001 - 统一降级
                last_err = e
                if with_json_mode:
                    continue
                break
        raise AIError(f"AI 调用失败: {last_err}")

    def chat_json(self, system, user, temperature=0.2):
        """发起对话并解析出 JSON 对象"""
        return extract_json(self.chat(system, user, temperature=temperature))


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def extract_json(text):
    """从模型回复里抠出 JSON（兼容 ```json 包裹与前后废话）"""
    if not text:
        raise AIError("AI 返回为空")
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退而求其次：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise AIError("AI 返回不是合法 JSON")


# --------------------------------------------------------------------------
# ① 意图解析：自然语言 → 结构化检索意图
# --------------------------------------------------------------------------

_PARSE_SYSTEM = """你是影视资源检索助手。用户会给你一句想追更的剧集描述，请解析成结构化检索意图。

只输出 JSON，不要任何解释，格式：
{
  "title": "剧名（去掉季/年份等修饰，用于匹配媒体库）",
  "season": 4,
  "category": "电视剧",
  "aliases": ["可能的别名或简称"],
  "queries": ["用于调搜索接口的关键词，按优先级从精确到宽泛，2-4个"]
}

规则：
- title 只保留剧名主体，例如「现在就出发 第四季」→ title 为「现在就出发」，season 为 4。
- season 识别不到就填 null。
- category 只能从这五个里选一个：电影、电视剧、动漫、综艺、纪录片。按剧名判断：
  动画/番剧/国漫/日漫类选「动漫」；真人连续剧、韩剧、美剧选「电视剧」；
  单部影片选「电影」；真人秀、脱口秀、竞技、访谈、真人户外类选「综艺」；
  自然/历史/科学纪实类选「纪录片」。拿不准时选「电视剧」。
- queries 要包含「剧名+季」和「剧名」两种，供搜索接口多次尝试。
- 只输出 JSON。"""


def parse_intent(keyword, client=None):
    """
    解析用户输入。AI 不可用时走本地规则，保证功能可用。
    返回 {title, season, aliases, queries, source}
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"title": "", "season": None, "aliases": [], "queries": [], "source": "empty"}

    if client is not None and client.ready:
        try:
            raw = client.chat_json(_PARSE_SYSTEM, keyword, temperature=0)
            intent = _normalize_intent(raw, keyword)
            intent["source"] = "ai"
            return intent
        except Exception:  # noqa: BLE001 - 降级
            pass

    return fallback_parse_intent(keyword)


def fallback_parse_intent(keyword):
    """本地启发式解析（无 AI 时使用）"""
    keyword = (keyword or "").strip()
    if not keyword:
        return {
            "title": "", "season": None, "aliases": [], "queries": [], "source": "local",
        }

    season = None
    for pattern in _SEASON_PATTERNS:
        m = re.search(pattern, keyword, re.I)
        if not m:
            continue
        token = m.group(1)
        if token.isdigit():
            season = int(token)
        elif token in _CN_NUM:
            season = _CN_NUM[token]
        if season:
            break

    title = keyword
    if season:
        title = re.sub(r"第\s*[0-9一二三四五六七八九十]+\s*[季部]", " ", title)
        title = re.sub(r"(?i)\bseason\s*\d{1,2}\b", " ", title)
        title = re.sub(r"(?i)\bS\d{1,2}\b", " ", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -_·")

    queries = []
    if season:
        queries.append(f"{title} 第{season}季")
    queries.append(title or keyword)
    if title and title != keyword:
        queries.append(keyword)

    seen, uniq = set(), []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)

    return {
        "title": title or keyword,
        "season": season,
        "category": guess_category(keyword),
        "aliases": [],
        "queries": uniq or [keyword],
        "source": "local",
    }


def _normalize_intent(raw, keyword):
    """把模型返回规整成固定结构"""
    if not isinstance(raw, dict):
        raise AIError("意图解析返回结构异常")

    title = (raw.get("title") or "").strip()
    season = raw.get("season")
    if isinstance(season, str) and season.isdigit():
        season = int(season)
    if not isinstance(season, int) or season <= 0:
        season = None

    category = str(raw.get("category") or "").strip()
    if category not in MEDIA_TYPES:
        category = ""

    # 模型有时把数组字段返回成字符串（"queries": "剧名 第1季"），
    # 直接迭代会逐字符拆分，这里先归一成列表
    aliases_raw = raw.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases_raw = [aliases_raw]
    queries_raw = raw.get("queries") or []
    if isinstance(queries_raw, str):
        queries_raw = [queries_raw]

    aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]
    queries = [str(q).strip() for q in queries_raw if str(q).strip()]

    if not title:
        raise AIError("意图解析缺少 title")
    if not queries:
        queries = [title]

    # 保证宽泛词也在检索词里，避免只搜到精确词而漏资源
    if title not in queries:
        queries.append(title)

    seen, uniq = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            uniq.append(q)

    return {"title": title, "season": season, "category": category,
            "aliases": aliases, "queries": uniq[:5]}


# --------------------------------------------------------------------------
# ①-b 媒体类型与目标文件夹匹配
# --------------------------------------------------------------------------

MEDIA_TYPES = ["电视剧", "电影", "动漫", "综艺", "纪录片"]

# 分类关键词（无 AI 时的启发式兜底，拿不准返回空串由调用方决定默认值）
_CATEGORY_HINTS = [
    ("动漫", ["动漫", "动画", "番剧", "国漫", "日漫", "新番"]),
    ("综艺", ["综艺", "真人秀", "脱口秀", "访谈", "选秀", "大赛"]),
    ("纪录片", ["纪录片", "纪实"]),
    ("电影", ["电影", "影片"]),
]

# 目录名里的季标记：S02 / 第二季 / 第2季 / 尾部裸数字（如「喜人奇妙夜2」）
_SEASON_IN_NAME = re.compile(
    r"(?:[.\s_\-]*[Ss](\d{1,2}))$"
    r"|(?:[.\s_\-]*第\s*([0-9一二三四五六七八九十]+)\s*季)$"
    r"|(?<=[^\d])(\d{1,2})$"
)


def guess_category(text):
    """无 AI 时按关键词猜类型；猜不到返回空串"""
    text = (text or "").lower()
    for category, words in _CATEGORY_HINTS:
        if any(w in text for w in words):
            return category
    return ""


def normalize_name(name):
    """归一化名称用于比较：去空白与常见标点、统一小写"""
    name = (name or "").strip().lower()
    return re.sub(r"[\s\-_.·,，。:：;；!！?？'\"“”‘’()（）\[\]【】《》<>]+", "", name)


def split_season(name):
    """
    把目录名拆成 (主体, 季号)，识别不到季号时返回 (原名, None)。
    让「中餐厅」能匹配上「中餐厅S09」、「喜人奇妙夜」能匹配上「喜人奇妙夜2」。
    """
    name = (name or "").strip()
    m = _SEASON_IN_NAME.search(name)
    if not m:
        return name, None
    token = m.group(1) or m.group(2) or m.group(3)
    season = int(token) if token and token.isdigit() else _CN_NUM.get(token or "")
    if not season:
        return name, None
    stem = name[: m.start()].strip(" ._-·")
    # 裸季号目录（S03 / 第四季）主体为空是正常的；但只剩一两个字的主体
    # 说明这个数字多半不是季标记（如「五哈2」里的 2 也可能是季号，保守起见仍按季号处理）
    if stem and len(normalize_name(stem)) < 2:
        return name, None
    return stem, season


def match_folder(title, aliases, dir_names, threshold=0.75, season=None):
    """
    在候选目录名里找最匹配剧名的那一个（「历史文件夹」）。
    返回 (命中的目录名, 置信度, 命中说明)；没找到时第一项为 None。
    season 给定时参与打分：季号一致加分，季号不符重罚（避免「第十季」匹配到 S09）。
    """
    if not title or not dir_names:
        return None, 0.0, ""

    keys = []
    base = normalize_name(title)
    if base:
        keys.append((base, "剧名"))
    for alias in aliases or []:
        na = normalize_name(alias)
        if na and na != base:
            keys.append((na, "别名"))

    best = (None, 0.0, "")
    for dir_name in dir_names:
        stem, cand_season = split_season(dir_name)
        cand_full = normalize_name(dir_name)
        cand_stem = normalize_name(stem)
        for key, how in keys:
            if not key:
                continue
            if key == cand_full:
                score, way = 1.0, how + "完全一致"
            elif key == cand_stem:
                score, way = 0.95, how + "一致（目录名带季号）"
            elif cand_stem and (cand_stem.startswith(key) or key.startswith(cand_stem)):
                ratio = min(len(key), len(cand_stem)) / max(len(key), len(cand_stem))
                score, way = 0.8 * ratio, how + "前缀匹配"
            elif key in cand_full or cand_full in key:
                ratio = min(len(key), len(cand_full)) / max(len(key), len(cand_full))
                score, way = 0.6 * ratio, how + "部分包含"
            else:
                continue
            if season:
                if cand_season == season:
                    score, way = min(1.0, score + 0.05), way + "·季号一致"
                elif cand_season:
                    score, way = score * 0.3, way + "·季号不符"
            if score > best[1]:
                best = (dir_name, score, way)
    if best[0] and best[1] < threshold:
        return None, best[1], best[2]
    return best


# --------------------------------------------------------------------------
# ② 候选链接排序
# --------------------------------------------------------------------------

_RANK_SYSTEM = """你是影视资源筛选助手。给定用户的检索意图和一批候选网盘分享链接，请判断哪些链接最可能包含用户想追的剧集。

只输出 JSON：
{
  "ranking": [
    {"index": 0, "score": 95, "reason": "标题含剧名与第四季，且为近期更新"}
  ]
}

规则：
- index 是候选列表下标，score 为 0-100 的匹配度。
- 优先：标题含剧名 + 季号匹配、集数/更新信息明确、时间较新。
- 明显无关（不同剧、综艺误匹配、只有片段）的给低分。
- 只输出 JSON，ranking 必须覆盖传入的每一个 index。"""


def rank_links(intent, candidates, client=None):
    """
    给候选链接排序。返回与入参等长的列表，每项附加 score / reason。
    AI 不可用时按关键词命中 + 时间新鲜度排序。
    """
    candidates = candidates or []
    if not candidates:
        return []

    ranked = None
    if client is not None and client.ready:
        try:
            payload = {
                "intent": {
                    "title": intent.get("title"),
                    "season": intent.get("season"),
                    "aliases": intent.get("aliases") or [],
                },
                "candidates": [
                    {
                        "index": i,
                        "taskname": (c.get("taskname") or "")[:80],
                        "source": c.get("source") or "",
                        "channel": c.get("channel") or "",
                        "datetime": c.get("datetime") or "",
                    }
                    for i, c in enumerate(candidates)
                ],
            }
            raw = client.chat_json(
                _RANK_SYSTEM, json.dumps(payload, ensure_ascii=False), temperature=0
            )
            ranked = _apply_ranking(candidates, raw)
        except Exception:  # noqa: BLE001 - 降级
            ranked = None

    if ranked is None:
        ranked = _heuristic_rank(intent, candidates)
        for item in ranked:
            item["rank_source"] = "local"
    else:
        for item in ranked:
            item["rank_source"] = "ai"

    ranked.sort(key=lambda x: x.get("score", 0), reverse=True)
    return ranked


def _apply_ranking(candidates, raw):
    """把模型返回的 ranking 合并回候选列表"""
    if not isinstance(raw, dict):
        return None
    rows = raw.get("ranking")
    if not isinstance(rows, list) or not rows:
        return None

    scores = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(candidates)):
            continue
        try:
            score = float(row.get("score"))
        except (TypeError, ValueError):
            score = 0.0
        scores[idx] = (
            max(0.0, min(100.0, score)),
            str(row.get("reason") or "")[:200],
        )

    if not scores:
        return None

    out = []
    for i, cand in enumerate(candidates):
        item = dict(cand)
        score, reason = scores.get(i, (0.0, "模型未给出评分"))
        item["score"] = score
        item["reason"] = reason
        out.append(item)
    return out


def _days_since(text):
    """距今天数（解析不出返回 None）"""
    from datetime import datetime, timezone
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - ts).days)


_QUALITY_WORDS = (
    ("remux", 4), ("2160p", 6), ("4k", 6), ("60fps", 3), ("高码", 3),
    ("hdr", 3), ("杜比", 3), ("内封", 2), ("合集", 2), ("完结", 2), ("全", 1),
)


def _heuristic_rank(intent, candidates):
    """
    本地排序（不调模型，0 成本、结果可复现）。

    打分：剧名/别名命中 +60；季号匹配 +25 / 不符 -30；
    画质版本词 0~10；发布时间新鲜度 0~15；来源权重 0~3。
    「分享里最新文件」这一项由调用方探测后再二次排序（模型看不到内容）。
    """
    title = str(intent.get("title") or "").strip()
    aliases = [str(a).strip() for a in (intent.get("aliases") or []) if a]
    season = intent.get("season")
    terms = [t.lower() for t in ([title] + aliases) if t]

    out = []
    for cand in candidates:
        name = cand.get("taskname") or ""
        low = name.lower()
        score = 0.0
        reasons = []

        hits = [t for t in terms if t and t in low]
        if hits:
            score += 60
            reasons.append("标题含「%s」" % hits[0])
        else:
            reasons.append("标题未命中剧名")

        m = re.search(r"第\s*([0-9一二三四五六七八九十]+)\s*季|(?<![A-Za-z])[Ss](\d{1,2})(?!\d)", name)
        cand_season = None
        if m:
            token = m.group(1) or m.group(2)
            cand_season = int(token) if token.isdigit() else _CN_NUM.get(token)
        if season:
            if cand_season == season:
                score += 25
                reasons.append("第%d季" % season)
            elif cand_season:
                score -= 30
                reasons.append("季号不符（第%d季）" % cand_season)

        quality = 0
        for word, add in _QUALITY_WORDS:
            if word in low:
                quality += add
        if quality:
            score += min(quality, 10)
            reasons.append("画质/版本好")

        days = _days_since(cand.get("datetime"))
        if days is not None:
            score += max(0.0, 15.0 - min(days, 15))
            if days <= 3:
                reasons.append("近 %d 天发布" % days)

        source = (cand.get("source") or "").lower()
        if source.startswith("tg"):
            score += 3
        elif source.startswith("plugin"):
            score += 1

        item = dict(cand)
        item["score"] = max(0.0, min(score, 99.0))
        item["reason"] = "，".join(reasons)
        out.append(item)
    return out


def suggest_rename_rule(taskname, filenames, presets, apply_rule, client=None):
    """
    推荐重命名规则。

    参数：
      presets    : dict，形如 {名称: {"pattern":..., "replace":...}}，来自 config.magic_regex
      apply_rule : callable(pattern, replace, filename) -> 新文件名（由调用方注入 MagicRename）
    返回：
      {pattern, replace, reason, source, candidates:[{name,pattern,replace,preview,score}]}
    """
    filenames = [f for f in (filenames or []) if f]
    candidates = _build_rule_candidates(presets, apply_rule, filenames, taskname)
    if not candidates:
        return {
            "pattern": "",
            "replace": "",
            "reason": "没有可用的命名规则",
            "source": "none",
            "candidates": [],
        }

    # 撞名的规则不能推荐：多个文件算出同一个名字，转存时会互相覆盖。
    # 综艺一期分上下集时，把「第N期上/下」丢掉就必然撞名。
    safe = [c for c in candidates if not c.get("collision")]
    pool = safe or candidates
    best = max(pool, key=lambda c: c["score"])

    # 让 AI 在（不撞名的）候选里挑一个（并可微调模板）
    if client is not None and client.ready and filenames:
        try:
            payload = {
                "taskname": taskname,
                "sample_filenames": filenames[:20],
                "candidates": [
                    {"index": i, "name": c["name"], "pattern": c["pattern"],
                     "replace": c["replace"], "sample_preview": c["preview"][:5]}
                    for i, c in enumerate(pool)
                ],
            }
            raw = client.chat_json(
                _RULE_SYSTEM.replace("{vars}", " ".join(ALLOWED_VARS)),
                json.dumps(payload, ensure_ascii=False),
                temperature=0,
            )
            picked = _apply_rule_choice(raw, pool, apply_rule, filenames, taskname)
            if picked:
                # 模型可以微调模板，但微调后可能重新引入撞名（例如把「第N期上/下」
                # 换掉），这里复验一次，撞名就退回评分最高的安全候选
                chk = _score_rule(
                    "ai", picked.get("pattern") or "", picked.get("replace") or "",
                    apply_rule, filenames, taskname,
                )
                if not chk.get("collision"):
                    return picked
        except Exception:  # noqa: BLE001 - 降级
            pass

    return {
        "pattern": best["pattern"],
        "replace": best["replace"],
        "reason": best["reason"],
        "source": "local",
        "candidates": candidates,
    }


def _build_rule_candidates(presets, apply_rule, filenames, taskname):
    """把预设规则逐个试跑，按规范化程度打分"""
    out = []
    for name, cfg in (presets or {}).items():
        if not isinstance(cfg, dict):
            continue
        pattern = cfg.get("pattern") or ""
        replace = cfg.get("replace") or ""
        if not pattern:
            continue
        out.append(_score_rule(str(name), pattern, replace, apply_rule, filenames, taskname))

    # 内置兜底规则：库里常见形态「剧名.SxxExx.ext」
    builtin = {
        "name": "内置·规范命名",
        "pattern": r".*\.(mp4|mkv|mov|m4v|avi|ts)$",
        "replace": "{TASKNAME}.{SXX}E{E}.{EXT}",
    }
    out.append(
        _score_rule(
            builtin["name"], builtin["pattern"], builtin["replace"],
            apply_rule, filenames, taskname,
        )
    )
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def _score_rule(name, pattern, replace, apply_rule, filenames, taskname):
    """试跑一条规则并打分"""
    preview = []
    normalized = 0
    changed = 0
    compiled_ok = True
    try:
        re.compile(pattern)
    except re.error:
        compiled_ok = False

    if compiled_ok:
        for fn in filenames[:30]:
            try:
                new_name = apply_rule(pattern, replace, fn)
            except Exception:  # noqa: BLE001
                new_name = fn
            preview.append({"from": fn, "to": new_name})
            if re.search(r"S\d{1,2}E\d{1,3}", new_name, re.I):
                normalized += 1
            if new_name != fn:
                changed += 1

    total = max(1, len(filenames[:30]))
    score = 0.0
    if compiled_ok:
        # 撞名是硬伤：多个文件算出同一个名字，转存时会互相覆盖
        unique_names = len({p["to"] for p in preview})
        collision = len(preview) - unique_names
        # 保留「第N期 / 上 / 下 / 加更」这类区分信息，综艺靠它区分同一期的上下集
        kept_desc = sum(
            1 for p in preview
            if re.search(r"第\s*\d+\s*期|[上中下]|加更|花絮|先导|回顾|特辑", p["to"])
        )
        score += (normalized / total) * 40      # 规范成 SxxExx 的比例
        score += (changed / total) * 15         # 确实生效
        score += (kept_desc / total) * 25       # 保留了区分信息
        score += (unique_names / total) * 30    # 不撞名
        score -= collision * 30                 # 撞名重罚
        if "{TASKNAME}" in replace:
            score += 10                         # 带剧名的命名更利于刮削
    else:
        score = -100

    return {
        "name": name,
        "pattern": pattern,
        "replace": replace,
        "preview": preview,
        "score": round(score, 2),
        "collision": collision if compiled_ok else 0,
        "reason": (
            f"规范化 {normalized}/{total} 个文件"
            + (f"，但有 {collision} 处重名" if compiled_ok and collision else "")
            + (f"，保留区分信息 {kept_desc}/{total}" if compiled_ok else "")
        ),
    }


def _apply_rule_choice(raw, candidates, apply_rule, filenames, taskname):
    """校验并采纳模型的选择"""
    if not isinstance(raw, dict):
        return None
    try:
        idx = int(raw.get("choice"))
    except (TypeError, ValueError):
        return None
    if not (0 <= idx < len(candidates)):
        return None

    chosen = candidates[idx]
    replace = (raw.get("replace_template") or "").strip() or chosen["replace"]

    # 模板安全校验：只允许已知变量，且能试跑成功
    if not _template_is_safe(replace):
        replace = chosen["replace"]

    scored = _score_rule(
        chosen["name"], chosen["pattern"], replace, apply_rule, filenames, taskname
    )
    return {
        "pattern": chosen["pattern"],
        "replace": replace,
        "reason": str(raw.get("reason") or scored["reason"])[:200],
        "source": "ai",
        "candidates": candidates,
    }


def _template_is_safe(template):
    """替换模板里不允许出现未声明的 {变量}"""
    if not template:
        return False
    leftover = re.sub(r"\{[A-Z]+\}", "", template)
    if "{" in leftover or "}" in leftover:
        return False
    for var in re.findall(r"\{[A-Z]+\}", template):
        if var not in ALLOWED_VARS:
            return False
    return True
