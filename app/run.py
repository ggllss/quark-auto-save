# !/usr/bin/env python3
# -*- coding: utf-8 -*-
from flask import (
    json,
    Flask,
    url_for,
    session,
    jsonify,
    request,
    redirect,
    Response,
    render_template,
    send_from_directory,
    stream_with_context,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from concurrent.futures import ThreadPoolExecutor, as_completed
from sdk.cloudsaver import CloudSaver
from sdk.pansou import PanSou
import contextlib

import ai
import tmdb
from datetime import timedelta
import subprocess
import requests
import hashlib
import logging
import traceback
import base64
import sys
import os
import re
import time

parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)
from quark_auto_save import Quark, Config, MagicRename

print(
    r"""
   ____    ___   _____
  / __ \  /   | / ___/
 / / / / / /| | \__ \
/ /_/ / / ___ |___/ /
\___\_\/_/  |_/____/

-- Quark-Auto-Save --
 """
)
sys.stdout.flush()


def get_app_ver():
    """获取应用版本"""
    try:
        with open("build.json", "r") as f:
            build_info = json.loads(f.read())
            BUILD_SHA = build_info["BUILD_SHA"]
            BUILD_TAG = build_info["BUILD_TAG"]
    except Exception as e:
        BUILD_SHA = os.getenv("BUILD_SHA", "")
        BUILD_TAG = os.getenv("BUILD_TAG", "")
    if BUILD_TAG[:1] == "v":
        return BUILD_TAG
    elif BUILD_SHA:
        return f"{BUILD_TAG}({BUILD_SHA[:7]})"
    else:
        return "dev"


# 文件路径
PYTHON_PATH = "python3" if os.path.exists("/usr/bin/python3") else "python"
SCRIPT_PATH = os.environ.get("SCRIPT_PATH", "./quark_auto_save.py")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "./config/quark_config.json")
PLUGIN_FLAGS = os.environ.get("PLUGIN_FLAGS", "")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = os.environ.get("PORT", 5005)
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", 1800))

config_data = {}
task_plugins_config_default = {}

app = Flask(__name__)
app.config["APP_VERSION"] = get_app_ver()
app.secret_key = "ca943f6db6dd34823d36ab08d8d6f65d"
app.config["SESSION_COOKIE_NAME"] = "QUARK_AUTO_SAVE_SESSION"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=31)
app.json.ensure_ascii = False
app.json.sort_keys = False
app.jinja_env.variable_start_string = "[["
app.jinja_env.variable_end_string = "]]"

scheduler = BackgroundScheduler()
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
# 过滤werkzeug日志输出
if not DEBUG:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("apscheduler").setLevel(logging.ERROR)
    sys.modules["flask.cli"].show_server_banner = lambda *x: None


def gen_md5(string):
    md5 = hashlib.md5()
    md5.update(string.encode("utf-8"))
    return md5.hexdigest()


def get_login_token():
    username = config_data["webui"]["username"]
    password = config_data["webui"]["password"]
    return gen_md5(f"token{username}{password}+-*/")[8:24]


def is_login():
    login_token = get_login_token()
    if session.get("token") == login_token or request.args.get("token") == login_token:
        return True
    else:
        return False


# 设置icon
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


# 登录页面
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = config_data["webui"]["username"]
        password = config_data["webui"]["password"]
        # 验证用户名和密码
        if (username == request.form.get("username")) and (
            password == request.form.get("password")
        ):
            logging.info(f">>> 用户 {username} 登录成功")
            session.permanent = True
            session["token"] = get_login_token()
            return redirect(url_for("index"))
        else:
            logging.info(f">>> 用户 {username} 登录失败")
            return render_template("login.html", message="登录失败")

    if is_login():
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


# 退出登录
@app.route("/logout")
def logout():
    session.pop("token", None)
    return redirect(url_for("login"))


# 管理页面
@app.route("/")
def index():
    if not is_login():
        return redirect(url_for("login"))
    return render_template(
        "index.html", version=app.config["APP_VERSION"], plugin_flags=PLUGIN_FLAGS
    )


# 获取配置数据
@app.route("/data")
def get_data():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    data = Config.read_json(CONFIG_PATH)
    del data["webui"]
    # AI 的 API Key 不回传给前端，只告知是否已配置
    ai_cfg = data.get("ai")
    if isinstance(ai_cfg, dict):
        ai_cfg["api_key_set"] = bool(ai_cfg.get("api_key"))
        ai_cfg["api_key"] = ""
    data["api_token"] = get_login_token()
    data["task_plugins_config_default"] = task_plugins_config_default
    return jsonify({"success": True, "data": data})


# 更新数据
@app.route("/update", methods=["POST"])
def update():
    global config_data
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    # 使用允许列表防止批量赋值攻击
    allowed_keys = ["cookie", "crontab", "push_config", "tasklist",
                    "magic_regex", "plugins", "source", "ai"]
    for key, value in request.json.items():
        if key not in allowed_keys:
            continue
        if key == "ai" and isinstance(value, dict):
            # api_key 留空表示「不修改」，避免前端拿到掩码后把真 Key 覆盖掉
            incoming = {k: v for k, v in value.items() if k != "api_key_set"}
            if not (incoming.get("api_key") or "").strip():
                incoming["api_key"] = (config_data.get("ai") or {}).get("api_key", "")
            config_data["ai"] = incoming
        else:
            config_data.update({key: value})
    Config.write_json(CONFIG_PATH, config_data)
    # 重新加载任务
    if reload_tasks():
        logging.info(f">>> 配置更新成功")
        return jsonify({"success": True, "message": "配置更新成功"})
    else:
        logging.info(f">>> 配置更新失败")
        return jsonify({"success": False, "message": "配置更新失败"})


# 处理运行脚本请求
@app.route("/run_script_now", methods=["POST"])
def run_script_now():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    tasklist = request.json.get("tasklist", [])
    command = [PYTHON_PATH, "-u", SCRIPT_PATH, CONFIG_PATH]
    logging.info(
        f">>> 手动运行任务 [{tasklist[0].get('taskname') if len(tasklist)>0 else 'ALL'}] 开始执行..."
    )

    def generate_output():
        # 设置环境变量
        process_env = os.environ.copy()
        process_env["PYTHONIOENCODING"] = "utf-8"
        if request.json.get("quark_test"):
            process_env["QUARK_TEST"] = "true"
            process_env["COOKIE"] = json.dumps(
                request.json.get("cookie", []), ensure_ascii=False
            )
            process_env["PUSH_CONFIG"] = json.dumps(
                request.json.get("push_config", {}), ensure_ascii=False
            )
        if tasklist:
            process_env["TASKLIST"] = json.dumps(tasklist, ensure_ascii=False)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=process_env,
        )
        try:
            for line in iter(process.stdout.readline, ""):
                logging.info(line.strip())
                yield f"data: {line}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            process.stdout.close()
            process.wait()

    return Response(
        stream_with_context(generate_output()),
        content_type="text/event-stream;charset=utf-8",
    )


# 季号写法：S02E01 / S02 / Season 2 / 第二季（「第2期」是综艺的期号，不算季）
_SEASON_RES = (
    re.compile(r"[Ss](\d{1,2})(?=[Ee]|[.\-_ \]]|$)"),
    re.compile(r"[Ss]eason\s*(\d{1,2})", re.I),
    re.compile(r"第\s*([0-9一二三四五六七八九十]{1,3})\s*季"),
)
_PERIOD_RE = re.compile(r"第\s*[0-9一二三四五六七八九十]{1,3}\s*[期集]")


# 常见媒体后缀：不去掉的话「xxx.mp4」里的 4 会被当成季号
_MEDIA_EXTS = (
    ".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".rmvb", ".flv", ".wmv",
    ".mov", ".mpg", ".mpeg", ".iso", ".strm", ".webm",
)


def _strip_media_ext(text):
    low = text.lower()
    for ext in _MEDIA_EXTS:
        if low.endswith(ext):
            return text[: -len(ext)]
    return text


def _season_of_text(text):
    """从一段文字里认出季号，认不出返回 None"""
    text = _strip_media_ext(str(text or "").strip())
    if not text:
        return None
    for pat in _SEASON_RES:
        m = pat.search(text)
        if not m:
            continue
        token = m.group(1)
        if token.isdigit():
            return int(token)
        season = ai._CN_NUM.get(token)
        if season:
            return season
    # 「第N期/第N集」是综艺的期号，不能当季号
    if _PERIOD_RE.search(text):
        return None
    # 分享常用「[王牌对王牌4][2019]」这种带括号的写法：
    # 先把括号里的 4 位年份摘掉，再剥掉括号本身，才认得出季号 4
    cleaned = re.sub(r"[(（\[【]\s*(?:19|20)\d{2}\s*[)）\]】]", " ", text)
    cleaned = re.sub(r"[()（）\[\]【】]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    _stem, season = ai.split_season(cleaned)
    if season:
        return season
    _stem, season = ai.split_season(text)
    return season


def _season_from_share(items):
    """
    从分享内容判断这是第几季。

    分享里的文件名/目录名一般自带季号（Breaking.Bad.S02E01、
    绝命毒师 第二季、喜人奇妙夜2），比去问 TMDB 可靠——
    分享本身就说清了是第几季。返回 (季号, 命中次数)。
    """
    votes = {}
    for it in items or []:
        pieces = []
        for text in (it.get("dir_path") or "", it.get("file_name") or ""):
            text = _strip_media_ext(str(text or ""))
            if not text:
                continue
            pieces.append(text)
            pieces.extend(p for p in text.split("/") if p)
        seen = set()
        for piece in pieces:
            piece = piece.strip()
            # 纯数字片段（01、2026）不是季号，跳过
            if not piece or re.fullmatch(r"[\d\s._-]+", piece):
                continue
            season = _season_of_text(piece)
            if season and season not in seen:
                seen.add(season)
                votes[season] = votes.get(season, 0) + 1
    if not votes:
        return None, 0
    season, hits = max(votes.items(), key=lambda kv: kv[1])
    # 只命中一两个文件时可能只是巧合（比如某一集的名字里带数字），不下结论
    return (season, hits) if hits >= 2 else (None, hits)


# Kodi/Emby 的辅助文件：海报、fanart、nfo、字幕等，不参与重命名
_AUX_EXTS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tbn",
    ".nfo", ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup",
    ".txt", ".md5", ".sfv", ".torrent",
    ".azw3", ".azw", ".epub", ".mobi", ".pdf", ".cbz", ".cbr",
    ".docx", ".doc", ".xlsx", ".pptx", ".exe", ".bat", ".url", ".lnk",
)


# 电影文件名里的 release 垃圾（分辨率/来源/编码/音轨/语言标记）
_MOVIE_JUNK_RE = re.compile(
    r"(2160p|1080p|720p|480p|4k|uhd|blu-?ray|bluray|remux|web-?dl|webrip|hdtv|"
    r"x265|x264|h265|h264|hevc|avc|10bit|8bit|hdr10|hdr|dolby|dovi|dv|"
    r"dts-?hd|dts|truehd|atmos|aac|ac3|eac3|flac|ma|5\.1|7\.1|2\.0|60fps|hfr|"
    r"proper|repack|extended|hq|国英双语|国语|英语|双语|内封|简英|中字|字幕|"
    r"蓝光|原盘|合集|臻彩|高码率|高码|无水印|纯净版|外挂|简中|繁中|中英|内嵌|硬字幕|"
    r"ddp|dd5|dd7|eac3|opus|lpcm|aac2|hdr10\+|hdr10|sdr|dv|国粤|粤语|普通话)",
    re.I,
)


def _movie_title_year(text):
    """
    从电影文件名/目录名里拆出 (片名, 年份)。

    The.Wandering.Earth.2019.2160p.WEB-DL.x265.mkv → ("The Wandering Earth", "2019")
    流浪地球2（2023）1080p.mkv                       → ("流浪地球2", "2023")
    """
    base = _strip_media_ext(str(text or ""))
    base = base.replace("_", " ").replace(".", " ")
    # 年份：优先取括号里的，其次取第一个 19xx/20xx
    year = ""
    m = re.search(r"[(\[【（［]\s*((?:19|20)\d{2})\s*[)\]】）］]", base)
    if m:
        year = m.group(1)
        base = base[: m.start()] + " " + base[m.end():]
    else:
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", base)
        if m:
            year = m.group(1)
            base = base[: m.start()]
    # 去掉字幕组/其它括号内容
    base = re.sub(r"[(\[【（［][^)\]】）］]*[)\]】）］]", " ", base)
    base = re.sub(r"[\s.\-_]+", " ", base).strip(" -_")
    base = _MOVIE_JUNK_RE.sub(" ", base)
    base = re.sub(r"\s+", " ", base)
    # 去掉尾部残留的连接符（「流浪地球2 & 」这种）
    base = base.strip(" -_&+、,，·")
    return base, year


def _movie_version(name):
    """电影版本标记：2160p / 1080p / 4K…（同一部片有多个版本时用来区分）"""
    m = re.search(r"(2160p|1080p|720p|480p|4k|uhd)", str(name or ""), re.I)
    return m.group(1).lower() if m else ""


def _is_aux_file(name):
    """海报/nfo/字幕这类辅助文件，不当作正片处理"""
    return os.path.splitext(str(name or ""))[1].lower() in _AUX_EXTS


def _air_date(name, dir_path=""):
    """
    从文件名/路径里还原播出日期（YYYY-MM-DD）。

    综艺常见两种写法：20250830.笑点浓缩版.mp4（带年）、
    12.08.mp4（只有月日，年份在上级目录 2023/ 里）。
    """
    text = _strip_media_ext(str(name or ""))
    date = tmdb.name_date(text)
    if date:
        return date
    m = re.search(r"(?<!\d)(\d{1,2})[.\-_月](\d{1,2})(?!\d)", text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year = ""
    for piece in reversed(str(dir_path or "").split("/")):
        y = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", piece)
        if y:
            year = y.group(1)
            break
    if not year:
        return None
    return "%s-%02d-%02d" % (year, month, day)


def _episode_from_name(name):
    """从文件名里认出集号/期号（09期 / 第9期 / S01E09 / E09）"""
    text = _strip_media_ext(str(name or ""))
    for pat in (
        r"第\s*(\d{1,3})\s*[期集话]",
        r"^\s*(\d{1,3})\s*[期集]",
        r"[Ss]\d{1,2}[Ee](\d{1,3})",
        r"(?:^|[. _\-])[Ee](\d{1,3})(?!\d)",
    ):
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def _season_of_path(dir_path):
    """
    从目录路径里找季号：从最内层往外找（越深越准）。

    「王牌对王牌1-9合集/1-8季/[王P对W牌7][2022]/」这种路径，
    最外层写的是 1-9 区间，只有最内层才是真的第 7 季。
    """
    for piece in reversed([p for p in str(dir_path or "").split("/") if p.strip()]):
        piece = piece.strip()
        if re.fullmatch(r"[\d\s._-]+", piece):
            continue          # 纯数字（年份/序号）
        if re.search(r"\d\s*[-~到]\s*\d", piece):
            continue          # 「1-9」「1~8」这种区间不是季号
        season = _season_of_text(piece)
        if season:
            return season
    return None


def _seasons_in_share(items):
    """分享里出现了哪些季（一个链接可能整季打包，S01…S05 各一个目录）"""
    seasons = set()
    for it in items or []:
        for text in (it.get("dir_path") or "", it.get("file_name") or ""):
            text = _strip_media_ext(str(text or ""))
            for piece in [text] + [x for x in text.split("/") if x]:
                if re.fullmatch(r"[\d\s._-]+", piece.strip() or "x"):
                    continue
                season = _season_of_text(piece)
                if season:
                    seasons.add(season)
    return seasons


def _pansou_servers(ps_data):
    """
    解析 PanSou 实例列表，支持三种写法：
      server: "https://a, https://b"      逗号/空格/分号分隔
      server: 换行分隔的多行
      servers: ["https://a", "https://b"]
    最多取 3 个，避免请求量爆炸。
    """
    raw = ps_data.get("servers")
    items = []
    if isinstance(raw, list):
        items.extend(str(x) for x in raw)
    text = ps_data.get("server")
    if isinstance(text, str):
        items.extend(re.split(r"[\s,;]+", text))
    out = []
    for item in items:
        item = (item or "").strip().rstrip("/")
        if item and item not in out:
            out.append(item)
    return out[:3]


def _collect_suggestions(query, deep=False):
    """聚合 PanSou + CloudSaver 的搜索结果，按时间倒序去重。

    普通搜索与 AI 搜索共用同一份实现，避免两处逻辑漂移。
    """
    cs_data = config_data.get("source", {}).get("cloudsaver", {})
    ps_data = config_data.get("source", {}).get("pansou", {})

    def cs_search():
        if (
            cs_data.get("server")
            and cs_data.get("username")
            and cs_data.get("password")
        ):
            cs = CloudSaver(cs_data.get("server"))
            cs.set_auth(
                cs_data.get("username", ""),
                cs_data.get("password", ""),
                cs_data.get("token", ""),
            )
            search = cs.auto_login_search(query)
            if search.get("success"):
                if search.get("new_token"):
                    cs_data["token"] = search.get("new_token")
                    Config.write_json(CONFIG_PATH, config_data)
                return cs.clean_search_results(search.get("data")) or []
        return []

    ps_servers = _pansou_servers(ps_data)

    def ps_search(server):
        """单个 PanSou 实例的搜索（失败返回空，不影响其它实例）"""
        try:
            return PanSou(server).search(query, deep) or []
        except Exception:  # noqa: BLE001
            return []

    search_results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        if str(cs_data.get("enable", "true")).lower() == "true":
            futures.append(executor.submit(cs_search))
        if str(ps_data.get("enable", "true")).lower() == "true":
            # 多个 PanSou 实例并行查：公共实例会限流/超时/索引轮换，
            # 多配几个（含自建）就能互相兜底
            for _srv in ps_servers:
                futures.append(executor.submit(ps_search, _srv))
        for future in as_completed(futures):
            search_results.extend(future.result() or [])

    # 按时间排序并去重
    search_results.sort(key=lambda x: x.get("datetime", ""), reverse=True)
    results = []
    link_array = []
    for item in search_results:
        url = item.get("shareurl", "")
        if url != "" and url not in link_array:
            link_array.append(url)
            results.append(item)
    return results


@app.route("/task_suggestions")
def get_task_suggestions():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    query = request.args.get("q", "").lower()
    deep = request.args.get("d", "").lower() == "1"
    try:
        return jsonify({"success": True, "data": _collect_suggestions(query, deep)})
    except Exception as e:
        return jsonify({"success": True, "message": f"error: {str(e)}"})


# ======================================================================
# AI 追剧
# 分工：模型只做「不确定」的语义判断（意图解析 / 候选排序 / 命名建议）；
#       「哪些集可更新」是确定性比对（MagicRename + 目标目录已有文件），
#       不交给模型，避免幻觉造成漏转或误判。
# ======================================================================


def _ai_client():
    return ai.AIClient(config_data.get("ai", {}))


def _require_cookie():
    """取出第一个账号，没有则返回 None"""
    cookie_list = config_data.get("cookie") or []
    if not cookie_list or not (cookie_list[0] or "").strip():
        return None
    return Quark(cookie_list[0])


def _match_task_by_title(title):
    """按剧名匹配已有任务，作为保存路径与命名规则的默认基准"""
    if not title:
        return None, ""
    title = title.strip()
    fuzzy = None
    for task in config_data.get("tasklist", []) or []:
        name = (task.get("taskname") or "").strip()
        if not name:
            continue
        if name == title:
            return task, "剧名完全一致"
        if fuzzy is None and (title in name or name in title):
            fuzzy = task
    if fuzzy is not None:
        return fuzzy, "剧名包含匹配"
    return None, ""


@app.route("/ai/status")
def ai_status():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    client = _ai_client()
    return jsonify(
        {
            "success": True,
            "data": {
                "enable": client.enable,
                "ready": client.ready,
                "base_url": client.base_url,
                "model": client.model,
                "has_key": bool(client.api_key),
            },
        }
    )


@app.route("/ai/test", methods=["POST"])
def ai_test():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        reply = _ai_client().chat(
            "你是连通性测试助手。", '只回复 JSON：{"ok": true}', temperature=0
        )
        return jsonify(
            {
                "success": True,
                "message": "AI 连接成功",
                "data": {"reply": reply[:200]},
            }
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"AI 连接失败：{e}"})


# --------------------------------------------------------------------------
# AI 追剧：媒体类型与入库路径解析
# --------------------------------------------------------------------------

_TYPE_NAMES = ["电视剧", "电影", "动漫", "综艺", "纪录片"]


def _infer_media_roots():
    """
    未显式配置类型根目录时，从现有任务的保存路径推断。
    例如任务存于 /0-影音/综艺/现在就出发/S04，可推出「综艺」根目录是 /0-影音/综艺，
    其余类型用同一父目录（/0-影音）补齐，这样开箱即可用。
    """
    found, base = {}, ""
    for task in (config_data.get("tasklist") or []):
        if not isinstance(task, dict):
            continue
        segments = [s for s in (task.get("savepath") or "").strip().split("/") if s]
        for i, seg in enumerate(segments):
            if seg in _TYPE_NAMES:
                found.setdefault(seg, "/" + "/".join(segments[: i + 1]))
                if i > 0 and not base:
                    base = "/" + "/".join(segments[:i])
                break
    if not found:
        return {}
    return {n: (found.get(n) or ((base + "/" + n) if base else "")) for n in _TYPE_NAMES}


def _media_roots():
    """各类型根目录：显式配置优先，缺的用推断值补齐"""
    configured = (config_data.get("ai") or {}).get("media_roots") or {}
    roots = {}
    if isinstance(configured, dict):
        roots = {k: (v or "").strip() for k, v in configured.items() if (v or "").strip()}
    inferred = _infer_media_roots()
    for name in _TYPE_NAMES:
        if not roots.get(name) and inferred.get(name):
            roots[name] = inferred[name]
    return roots


def _list_dir_items(path):
    """列出目录下的条目；目录不存在返回 None，读取失败抛异常"""
    account = _require_cookie()
    if account is None:
        raise RuntimeError("未配置 Cookie")
    fids = account.get_fids([path])
    if not fids:
        return None
    ret = account.ls_dir(fids[0]["fid"])
    return (ret.get("data", {}) or {}).get("list") or []


def _resolve_media_path(title, aliases=None, season=None, category=""):
    """
    按类型 + 剧名算出目标入库路径：
    - 在对应类型目录下找同名文件夹（已存在的「历史文件夹」），命中就存进去
    - 没命中就用剧名新建，并在返回里标明「将新建」
    - 分季时在剧集文件夹下再套一层 S0X（目录名自带相同季号则不重复套）
    """
    roots = _media_roots()
    aliases = aliases or []
    matched_folder, match_score, match_note = None, 0.0, ""
    category_source = "manual" if category in roots else ""

    # 没指定类型时，到各类型目录里找已有文件夹，命中即可反推类型。
    # 判类型时不带季号（同一部剧换季不会换类型），找具体文件夹时才用季号消歧。
    # 同时记下该剧在库里的命名习惯：季号是写在文件夹名里（中餐厅S10），还是用 S0X 子目录。
    season_in_name, style_stem = False, ""
    if not category and title:
        best_type = None
        for name in _TYPE_NAMES:
            root = roots.get(name)
            if not root:
                continue
            try:
                items = _list_dir_items(root)
            except Exception:
                continue
            if items is None:
                continue
            dirs = [f["file_name"] for f in items if f.get("dir")]
            hit, score, note = ai.match_folder(title, aliases, dirs)
            if hit and (best_type is None or score > best_type[1]):
                best_type = (name, score, hit, note)
        if best_type:
            category, category_source = best_type[0], "existing_folder"
            stem, dir_season = ai.split_season(best_type[2])
            if dir_season:
                season_in_name, style_stem = True, stem

    if category not in roots:
        category = "电视剧" if "电视剧" in roots else (next(iter(roots), "") or "电视剧")
        if not category_source:
            category_source = "default"

    result = {
        "category": category,
        "category_source": category_source,
        "season": season,
        "root": roots.get(category, ""),
        "roots": roots,
        "matched_folder": matched_folder,
        "match_score": round(match_score, 2),
        "match_note": match_note,
        "folder_exists": bool(matched_folder),
        "season_dir": "",
        "season_exists": False,
        "savepath": "",
        "create_dirs": [],
        "note": "",
    }
    root = result["root"]
    if not root:
        result["note"] = "未找到「%s」的根目录，请在 AI 设置里填写各类型目录" % category
        return result

    folder_name = ""
    if title:
        try:
            items = _list_dir_items(root)
        except Exception as e:
            result["note"] = "读取类型目录失败：%s" % e
            return result
        if items is None:
            result["note"] = "类型目录不存在：%s" % root
            return result
        dirs = [f["file_name"] for f in items if f.get("dir")]
        hit, score, note = ai.match_folder(title, aliases, dirs, season=season)
        result["match_score"] = round(score, 2)
        result["match_note"] = note or "未找到同名文件夹"
        if hit:
            folder_name = hit
            result.update({"matched_folder": hit, "folder_exists": True})
        elif season and season_in_name and style_stem:
            # 库里这部剧本季是写在文件夹名里的（如「中餐厅S10」），新一季沿用同样形式，
            # 避免出现「中餐厅S09」与「中餐厅/S11」两种风格并存
            folder_name = "%sS%02d" % (style_stem, int(season))
            result["match_note"] = "沿用库内「剧名+季号」命名习惯"
        else:
            folder_name = title

    if not folder_name:
        result["note"] = "缺少剧名，无法确定入库目录"
        return result

    # 季号没给时自动推断「现在更新的是第几季」：
    # ① 命中的目录名本身带季号（中餐厅S10）→ 就是第 10 季
    # ② 该剧文件夹下已有 S0X 子目录 → 取最大的那个（正在更新的那季）
    season_source = "keyword" if season else ""
    if not season and result["folder_exists"]:
        _stem, folder_season = ai.split_season(folder_name)
        if folder_season:
            season, season_source = folder_season, "folder_name"
        else:
            try:
                sub_items = _list_dir_items("/".join([root.rstrip("/"), folder_name])) or []
                seasons = []
                for it in sub_items:
                    if it.get("dir"):
                        _s, sub_season = ai.split_season(it.get("file_name") or "")
                        if sub_season:
                            seasons.append(sub_season)
                if seasons:
                    season, season_source = max(seasons), "library"
            except Exception:
                pass
    result["season"] = season
    result["season_source"] = season_source

    # 分季：目录名已经带相同季号就不再套一层，避免出现「中餐厅S09/S09」
    season_dir = ""
    if season:
        _, dir_season = ai.split_season(folder_name)
        if dir_season != season:
            season_dir = "S%02d" % int(season)
    result["season_dir"] = season_dir

    show_path = "/".join([root.rstrip("/"), folder_name])
    savepath = "/".join([show_path] + ([season_dir] if season_dir else []))
    result["savepath"] = savepath

    create_dirs = []
    if not result["folder_exists"]:
        create_dirs.append(show_path)
    if season_dir:
        season_exists = False
        if result["folder_exists"]:
            try:
                items = _list_dir_items(show_path) or []
                season_exists = any(
                    f.get("dir")
                    and ai.normalize_name(f["file_name"]) == ai.normalize_name(season_dir)
                    for f in items
                )
            except Exception:
                season_exists = False
        result["season_exists"] = season_exists
        if not season_exists:
            create_dirs.append(savepath)

    result["create_dirs"] = create_dirs
    if create_dirs:
        result["note"] = "将新建：" + "、".join(create_dirs)
    else:
        result["note"] = "命中已有文件夹「%s」（%s）" % (
            folder_name, result["match_note"] or "同名")
    return result


# 搜索结果缓存：同一关键词短时间内重复搜索直接复用，避免反复等公共搜索源
_SEARCH_CACHE = {}
_SEARCH_CACHE_TTL = 600  # 秒


def _search_cache_get(key):
    hit = _SEARCH_CACHE.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > _SEARCH_CACHE_TTL:
        _SEARCH_CACHE.pop(key, None)
        return None
    return value


def _search_cache_put(key, value):
    if len(_SEARCH_CACHE) > 60:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[key] = (time.time(), value)


def _search_cache_maybe_put(key, pool):
    """
    只缓存有结果的搜索。
    空结果多半是搜索源临时超时/限流造成的，缓存下来会让用户反复搜同一个词
    都拿到空结果，误以为「这部剧就是没有资源」。
    """
    if pool:
        _search_cache_put(key, pool)


def _name_is_relevant(title, aliases, candidate_name):
    """候选标题里是否真的含这部剧的名字，用来挡掉完全无关的链接"""
    name = ai.normalize_name(candidate_name)
    if not name:
        return False
    keys = [ai.normalize_name(title)]
    keys += [ai.normalize_name(a) for a in (aliases or [])]
    keys = [k for k in keys if k]
    if not keys:
        return True
    return any(k in name for k in keys)


# 链接有效性缓存：短时间内不会变，避免每次搜索都重新探测（探测很贵）
_ALIVE_CACHE = {}
_ALIVE_TTL = 1800


def _link_alive_cached(account, shareurl):
    hit = _ALIVE_CACHE.get(shareurl)
    if hit and time.time() - hit[0] < _ALIVE_TTL:
        return hit[1]
    value = _link_alive(account, shareurl)
    if len(_ALIVE_CACHE) > 500:
        _ALIVE_CACHE.clear()
    _ALIVE_CACHE[shareurl] = (time.time(), value)
    return value


def _link_alive(account, shareurl):
    """
    检查分享链接是否仍然有效。
    返回 True=有效 / False=已失效 / None=网络异常无法判断（保留，避免误杀）
    """
    try:
        pwd_id, passcode, _pdir, _ = account.extract_url(shareurl)
        ret = account.get_stoken(pwd_id, passcode)
        status = ret.get("status")
        if status == 200:
            return True
        if status == 500:  # 网络异常，判断不了
            return None
        return False
    except Exception:
        return None


def _run_plugins(plugins, targets, base_task, account, tree, buf):
    """
    按目标目录逐个触发插件。

    SmartStrm 这类插件一次只扫一个目录，而花絮会落到 S00、正片落到 S04，
    两个目录都得各自扫一次，否则另一个目录不会生成 .strm。
    返回实际触发过的插件名列表。
    """
    triggered = []
    for plugin_name, plugin in plugins.items():
        if not (getattr(plugin, "is_active", False) and hasattr(plugin, "run")):
            continue
        triggered.append(plugin_name)
        for path in targets:
            task = dict(base_task)
            task["savepath"] = path
            try:
                with contextlib.redirect_stdout(buf):
                    plugin.run(task, account=account, tree=tree)
            except Exception as e:  # noqa: BLE001 - 单个插件失败不影响入库
                buf.write("  插件 %s 处理 %s 出错：%s\n" % (plugin_name, path, e))
    return triggered


def _source_unreachable():
    """
    粗略判断已启用的搜索源服务是否连得上（只做 TCP 连接，2 秒超时）。
    返回连不上的源名称列表，用于把「源挂了」和「确实没资源」区分开。
    """
    import socket
    from urllib.parse import urlparse

    src = config_data.get("source", {}) or {}
    dead = []
    targets = []
    for key, label in (("pansou", "PanSou"), ("cloudsaver", "CloudSaver")):
        cfg = src.get(key) or {}
        if str(cfg.get("enable", "true")).lower() != "true":
            continue
        servers = _pansou_servers(cfg) if key == "pansou" else [
            str(cfg.get("server") or "").strip().rstrip("/")
        ]
        for srv in servers:
            if srv:
                targets.append((label, srv))
    for label, srv in targets:
        try:
            parsed = urlparse(srv)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not host:
                continue
            conn = socket.create_connection((host, port), timeout=2)
            conn.close()
        except Exception:
            dead.append(label)
    # 去重保序
    out = []
    for d in dead:
        if d not in out:
            out.append(d)
    return out


def _enabled_sources():
    """当前已启用的搜索源名称列表"""
    src = config_data.get("source", {}) or {}
    names = []
    for key, label in (("pansou", "PanSou"), ("cloudsaver", "CloudSaver")):
        cfg = src.get(key) or {}
        if str(cfg.get("enable", "true")).lower() == "true" and cfg.get("server"):
            names.append(label)
    return names


@app.route("/ai/resolve_path", methods=["POST"])
def ai_resolve_path():
    """按类型 + 剧名解析入库路径：命中已有文件夹，还是需要新建；分季是否要套 S0X"""
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    body = request.json or {}
    title = (body.get("title") or "").strip()
    aliases = body.get("aliases") or []
    category = (body.get("category") or "").strip()
    season = body.get("season")
    if season in ("", "null", None):
        season = None
    else:
        try:
            season = int(season)
        except (TypeError, ValueError):
            season = None
    try:
        data = _resolve_media_path(title, aliases, season, category)
    except Exception as e:
        return jsonify({"success": False, "message": f"解析入库路径失败：{e}"})
    data["category_options"] = _TYPE_NAMES
    # 推荐规则预设：命中的任务优先（沿用它的命名习惯），
    # 否则按类型选——综艺一期分上下集用 $ZONGYI_MAGIC，剧集用 $TV_MAGIC
    suggested = ""
    suggested_replace = ""
    try:
        _task, _ = _match_task_by_title(title)
        if _task and (_task.get("pattern") or "").strip():
            suggested = _task["pattern"].strip()
            suggested_replace = _task.get("replace") or ""
    except Exception:  # noqa: BLE001
        pass
    if not suggested:
        presets = config_data.get("magic_regex") or {}
        cat = data.get("category") or category
        if cat == "综艺" and "$ZONGYI_MAGIC" in presets:
            suggested = "$ZONGYI_MAGIC"
        elif "$TV_MAGIC" in presets:
            suggested = "$TV_MAGIC"
        if suggested:
            suggested_replace = (presets.get(suggested) or {}).get("replace") or ""
    data["suggested_pattern"] = suggested
    data["suggested_replace"] = suggested_replace
    return jsonify({"success": True, "data": data})


@app.route("/ai/search", methods=["POST"])
def ai_search():
    """剧名 → 意图解析 → 复用现有搜索 → AI 排序"""
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    body = request.json or {}
    keyword = (body.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"success": False, "message": "请输入剧集名称"})

    client = _ai_client()
    # ① 代码先算（瞬时）：拆季号、剥年份、生成检索词
    intent = ai.parse_intent(keyword, None)
    # ② 类型和中英别名交给 TMDB（一次查询、缓存 1 小时），比模型稳定
    _tmdb_key = (config_data.get("tmdb") or {}).get("api_key") or ""
    if _tmdb_key and intent.get("title"):
        try:
            _kind = tmdb.media_kind(_tmdb_key, intent["title"])
            _cat = tmdb.category_from_kind(_kind)
            if _cat:
                intent["category"] = _cat
            # 只要英文或中文别名——TMDB 会返回一堆冰岛语/俄语译名，
            # 拿去搜网盘只会增加无效查询
            _raw_alts = tmdb.alternative_titles(_tmdb_key, intent["title"])
            _alts = tmdb.pick_aliases(_raw_alts, limit=6)
            # 原始名（英文原名）最有用：英文标题的分享只认它
            _orig = (_kind or {}).get("original_name") or ""
            if _orig and _orig.lower() != intent["title"].lower():
                _alts = [_orig] + [a for a in _alts if a.lower() != _orig.lower()]
            _alts = [a for a in _alts if a.lower() != intent["title"].lower()][:6]
            if _alts:
                intent["aliases"] = _alts
                _q = list(intent.get("queries") or [])
                for _a in _alts[:2]:
                    if _a not in _q:
                        _q.append(_a)
                intent["queries"] = _q[:5]
            if _cat or _alts:
                intent["source"] = "local+tmdb"
        except Exception:  # noqa: BLE001 - TMDB 不可用不影响搜索
            pass
    # ③ 只有这几种情况才让模型兜底（罕见）：
    #    代码算不出标题 / TMDB 查不到类型 / 纯英文片名且 TMDB 没给中文别名
    #    （最后一种正是模型的强项：The Knockout → 狂飙）
    _need_ai = (
        not intent.get("title")
        or not intent.get("category")
        or (
            not intent.get("aliases")
            and re.search(r"[A-Za-z]{3,}", intent.get("title") or "")
            and not re.search(r"[\u4e00-\u9fff]", intent.get("title") or "")
        )
    )
    if _need_ai:
        intent = ai.parse_intent(keyword, client)
    queries = [(q or "").lower() for q in (intent.get("queries") or [])[:3] if q]

    def gather():
        pool, seen = [], set()
        if not queries:
            return pool
        # 多个检索词并行打源：串行时最坏要等 N 倍，并行只等最慢的那个
        with ThreadPoolExecutor(max_workers=min(3, len(queries))) as ex:
            futures = [ex.submit(_collect_suggestions, q, False) for q in queries]
            for fut in as_completed(futures):
                try:
                    items = fut.result() or []
                except Exception:
                    continue
                for item in items:
                    url = item.get("shareurl") or ""
                    if url and url not in seen:
                        seen.add(url)
                        pool.append(item)
        return pool

    # 缓存键要带上检索词：别名策略一变（比如补了英文原名），
    # 旧缓存就会一直返回「只有 2 条」的老结果
    cache_key = "|".join(
        [keyword.lower(), intent.get("title") or "", ",".join(sorted(queries))]
    )
    cached = _search_cache_get(cache_key)
    retried = False
    if cached is not None:
        pool, from_cache = cached, True
    else:
        from_cache = False
        # 公共搜索源不稳定（实测 PanSou 成功率约 40%：有时 0.7 秒就返回空、
        # 有时十几秒才超时）。空结果时多试几次：快速失败几乎不花时间，
        # 慢的情况由总时长预算兜住，避免用户一次搜不到就以为没资源。
        t0 = time.time()
        pool, attempts = [], 0
        while True:
            attempts += 1
            pool = gather()
            if pool:
                break
            if attempts >= 3 or (time.time() - t0) >= 40:
                break
        retried = attempts > 1
        _search_cache_maybe_put(cache_key, pool)

    # ① 相关性过滤：标题里完全不含剧名/别名的，直接挡掉
    title = intent.get("title") or keyword
    aliases = intent.get("aliases") or []
    relevant = [c for c in pool if _name_is_relevant(title, aliases, c.get("taskname") or "")]
    dropped_irrelevant = len(pool) - len(relevant)

    # ② 排序：先用启发式（0 成本）粗排，再让 AI 精排。
    # 不再按模型评分做绝对阈值过滤——实测模型打分普遍偏低，按分数砍会把结果砍空；
    # 「是否相关」由标题过滤保证，「是否有效」由存活检测保证。
    def _fresh_bonus(dt_text):
        """新鲜度加分：搜索索引里躺着大量旧帖，旧帖的分享多已失效，
        把新发布的顶上来能显著减少「点进去发现失效」。"""
        try:
            t = datetime.fromisoformat(str(dt_text).replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - t.astimezone(timezone.utc)).days
        except Exception:  # noqa: BLE001
            return 0
        if days <= 3:
            return 15
        if days <= 7:
            return 10
        if days <= 30:
            return 5
        return 0

    dropped_low_score = 0
    shortlist = []
    if relevant:
        heuristic = ai.rank_links(intent, relevant, None)
        for _c in heuristic:
            _c["fresh_bonus"] = _fresh_bonus(_c.get("datetime"))
        heuristic.sort(key=lambda c: -((c.get("score") or 0) + c.get("fresh_bonus", 0)))
        shortlist = heuristic[:24]

    # ③ AI 精排与存活检测互不依赖，并行跑——串行会白等一倍时间
    # （本地模型排序实测 7 秒，存活检测 5 秒左右）
    ranked = shortlist
    alive_flags = {}
    dropped_dead = 0
    candidates = []
    if shortlist:
        account = _require_cookie()

        def _check_alive():
            if account is None:
                return {}
            flags = {}
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {
                    pool.submit(_link_alive_cached, account, c["shareurl"]): c
                    for c in shortlist
                }
                for fut in as_completed(futures):
                    item = futures[fut]
                    try:
                        flags[item["shareurl"]] = fut.result()
                    except Exception:  # noqa: BLE001
                        flags[item["shareurl"]] = None
            return flags

        # 排序默认走纯代码（0 成本、可复现、结果稳定）；ai.rank_mode="model" 才调模型。
        _rank_mode = str((config_data.get("ai") or {}).get("rank_mode") or "code").lower()
        _rank_client = client if _rank_mode == "model" else None
        try:
            ranked = ai.rank_links(intent, shortlist, _rank_client) or shortlist
        except Exception:  # noqa: BLE001 - 排序失败就用原顺序
            ranked = shortlist

        # ⑤「看最新」：标题看不出它更新到哪一集，直接读一遍分享内容，
        # 把真正有新内容的分享顶到前面。
        # （实测：含「第2期上」的是几个标题写「第三季」的全季合集，只按标题排序会沉底）
        # 和存活检测并行——两者都要发网络请求，串行会白等一倍时间。
        def _probe_latest():
            probe_acc = _require_cookie()
            if probe_acc is None:
                return

            def _probe(c):
                try:
                    info = _share_latest(probe_acc, c["shareurl"], max_files=60)
                except Exception:  # noqa: BLE001
                    return
                latest = info.get("latest") or []
                if latest:
                    c["latest_file"] = latest[0].get("file_name") or ""
                    c["latest_date"] = latest[0].get("date") or ""
                    c["latest_total"] = info.get("total")

            with ThreadPoolExecutor(max_workers=8) as probe_pool:
                list(probe_pool.map(_probe, ranked[:10]))

        with ThreadPoolExecutor(max_workers=2) as outer:
            fut_alive = outer.submit(_check_alive)
            fut_probe = outer.submit(_probe_latest)
            try:
                alive_flags = fut_alive.result() or {}
            except Exception:  # noqa: BLE001
                alive_flags = {}
            try:
                fut_probe.result()
            except Exception:  # noqa: BLE001
                pass

        for c in ranked:
            if alive_flags.get(c["shareurl"]) is False:
                dropped_dead += 1
            else:
                candidates.append(c)
        # 内容最新（分享里最新文件的日期）排前面，同日期按相关度
        candidates.sort(
            key=lambda c: (c.get("latest_date") or "", c.get("score") or 0),
            reverse=True,
        )

    task, reason = _match_task_by_title(intent.get("title") or "")
    baseline = None
    if task:
        baseline = {
            "taskname": task.get("taskname"),
            "savepath": task.get("savepath") or "/",
            "pattern": task.get("pattern") or "",
            "replace": task.get("replace") or "",
            "match_reason": reason,
        }

    sources = _enabled_sources()
    try:
        resolved = _resolve_media_path(
            intent.get("title"), intent.get("aliases"),
            intent.get("season"), intent.get("category") or "",
        )
    except Exception as e:
        resolved = {"note": f"解析入库路径失败：{e}", "savepath": "", "create_dirs": []}
    hint = ""
    if not pool:
        if not sources:
            hint = "未启用任何搜索源，请到「系统配置 → 搜索源」中配置 PanSou 或 CloudSaver。"
        else:
            dead = _source_unreachable()
            if dead:
                hint = (
                    "搜索源（%s）连不上：请检查对应服务是否在运行（配置见「系统配置 → 搜索源」）。"
                    % "、".join(dead)
                )
            else:
                hint = (
                    "搜索源（%s）试了 %d 次都没有返回结果：公共搜索源此刻可能不稳定或被限流。"
                    "等几秒再点一次搜索通常就有结果。" % ("、".join(sources), attempts)
                )
    elif not candidates:
        hint = (
            "搜到 %d 条，但都不是这部剧的资源（已按剧名过滤，并剔除了失效链接）。"
            "换个更准确的名字试试，或在设置里手动填保存路径。" % len(pool)
        )

    return jsonify(
        {
            "success": True,
            "data": {
                "keyword": keyword,
                "intent": intent,
                "ai_used": intent.get("source") == "ai",
                "sources": sources,
                "retried": retried,
                "attempts": 0 if from_cache else attempts,
                "from_cache": from_cache,
                "pool_size": len(pool),
                "filtered": {
                    "irrelevant": dropped_irrelevant,
                    "dead": dropped_dead,
                    "low_score": dropped_low_score,
                },
                "candidates": candidates,
                "baseline": baseline,
                "resolved": resolved,
                "hint": hint,
            },
        }
    )


def _expand_share_files(account, pwd_id, stoken, root_fid="", max_depth=4, max_files=600):
    """
    递归展开分享里的文件夹，返回「文件级」条目（带所在目录）。

    分享常见结构是「剧名/S04/E01.mkv」这种嵌套，用户要选的是哪几集，
    而不是「哪个文件夹」。超过深度或数量上限的文件夹原样保留为一条，
    并标记 expanded=False，避免静默丢掉内容。
    返回 (条目列表, 是否被截断)
    """
    items, truncated = [], False

    def walk(fid, path, depth):
        nonlocal truncated
        if len(items) >= max_files:
            truncated = True
            return
        try:
            detail = account.get_detail(pwd_id, stoken, fid)
        except Exception:
            return
        if detail.get("code") != 0:
            return
        for it in (detail.get("data") or {}).get("list") or []:
            if len(items) >= max_files:
                truncated = True
                return
            name = it.get("file_name") or ""
            is_dir = bool(it.get("dir"))
            if is_dir and depth < max_depth:
                walk(it.get("fid"), path + name + "/", depth + 1)
                continue
            items.append(
                {
                    "fid": it.get("fid"),
                    "file_name": name,
                    "dir": is_dir,
                    "size": it.get("size"),
                    "updated_at": it.get("updated_at"),
                    "include_items": it.get("include_items"),
                    # 该文件在分享里所属目录的 fid（转存时要按目录分批）
                    "parent_fid": fid,
                    "dir_path": path,
                    "expanded": not is_dir,
                }
            )

    walk(root_fid, "", 0)
    return items, truncated


def _expand_share_files_smart(account, pwd_id, stoken, root_fid="", max_files=600):
    """
    两段式展开：先展开 2 层（多数分享够用，省掉几秒 API 往返）；
    如果还剩下没展开的目录（「合集/1-8季/第N季/年份/文件」这种 4 层结构），
    只把那些目录继续往下展开，不重扫整棵树。
    """
    items, truncated = _expand_share_files(
        account, pwd_id, stoken, root_fid, max_depth=2, max_files=max_files
    )
    pending = [i for i in items if i.get("dir") and not i.get("expanded")]
    if not pending:
        return items, truncated

    keep = [i for i in items if not (i.get("dir") and not i.get("expanded"))]
    for folder in pending:
        if len(keep) >= max_files:
            truncated = True
            break
        try:
            sub, sub_trunc = _expand_share_files(
                account, pwd_id, stoken, folder.get("fid") or "",
                max_depth=2, max_files=max_files - len(keep),
            )
        except Exception:  # noqa: BLE001 - 单个目录失败不影响其它
            sub, sub_trunc = [], False
        truncated = truncated or sub_trunc
        prefix = (folder.get("dir_path") or "") + (folder.get("file_name") or "") + "/"
        for item in sub:
            item["dir_path"] = prefix + (item.get("dir_path") or "")
        keep.extend(sub)
    return keep, truncated


def _extract_se(name, mr):
    """从文件名里提取 (季, 集)。提取不到返回 None。

    复用 MagicRename 的 {S}/{E} 变量正则，保证与改名引擎口径一致。
    """
    season = episode = None
    for pattern in mr.magic_variable.get("{S}", []):
        m = re.search(pattern, name)
        if m:
            try:
                season = int(m.group())
            except ValueError:
                season = None
            break
    for pattern in mr.magic_variable.get("{E}", []):
        m = re.search(pattern, name)
        if m:
            try:
                episode = int(m.group())
            except ValueError:
                episode = None
            break
    return season, episode


# 花絮类素材：这些不是正片，统一收进 S00（Special 季），
# Emby/Jellyfin 只有在 S00 里才会把它们当特辑刮削
EXTRA_MARKERS = (
    "先导", "纯享", "花絮", "加更", "预告", "彩蛋", "番外",
    "会员版", "特辑", "回顾", "未播", "幕后", "路透", "reaction",
)


def _is_extra_file(name):
    """是不是花絮类素材（先导/纯享/花絮/加更…）"""
    low = (name or "").lower()
    return any(m in low for m in EXTRA_MARKERS)


def extra_episode_number(name, episode_base=0, seq=0):
    """
    花絮素材该用第几集：
      - 文件名里有「第N期」且有集数起点 → 起点 + N - 1（和 TMDB 的集数对齐）
      - 有期号但没起点 → 直接用期号
      - 没有期号（先导片/回顾特辑等）→ 用顺序号
    """
    period = (content_key(name) or {}).get("period")
    if period and episode_base:
        return episode_base + period - 1
    if period:
        return period
    return seq


def _extra_label(name, taskname=""):
    """从花絮文件名里提取可读描述：去掉开头的日期和剧名前缀"""
    stem = os.path.splitext(name or "")[0]
    stem = re.sub(r"^20\d{2}[.\-_]?\d{1,2}[.\-_]?\d{1,2}\s*[-_. ]?\s*", "", stem)
    for prefix in (taskname, taskname + "4", taskname + "第四季"):
        if prefix and stem.startswith(prefix):
            stem = stem[len(prefix):].lstrip(".-_ ")
            break
    stem = re.sub(r'[\\/:*?"<>|]', " ", stem).strip()
    return stem[:60]


# 综艺/剧集的「内容信号」：这些才是判断「是不是同一集」的依据。
# 只看 E 号会误判——综艺一期分上下集，E 往往只是文件序号。
_CONTENT_TAGS = ("加更", "花絮", "先导", "回顾", "纯享", "预告", "特辑", "彩蛋", "番外", "会员版")


def content_key(name):
    """
    从文件名里提取内容信号：日期、期号、上中下、类型标记。
    返回 dict（可能为空 dict 表示提不出信号）。
    """
    text = name or ""
    key = {}
    m = re.search(r"(20\d{2})[.\-_]?(\d{2})[.\-_]?(\d{2})", text)
    if m:
        key["date"] = m.group(1) + m.group(2) + m.group(3)
    m = re.search(r"第\s*(\d{1,4})\s*期", text)
    if m:
        key["period"] = int(m.group(1))
    m = re.search(r"期\s*([上中下])", text) or re.search(r"([上中下])\s*(?:[.\-_ ]|$)", text)
    if m:
        key["part"] = m.group(1)
    tags = sorted({t for t in _CONTENT_TAGS if t in text})
    if tags:
        key["tags"] = tuple(tags)
    return key


def _same_content(a, b):
    """
    两个文件是不是同一集。

    期号、上下集、类型标记（加更/花絮/先导…）必须双方一致，缺省按空处理：
    否则「第1期」会被判成和「第1期加更」同一集。
    日期只在一方有日期时用于区分（库里的名字常常不带日期）。
    """
    if not a or not b:
        return False
    if a.get("period") != b.get("period"):
        return False
    if a.get("part") != b.get("part"):
        return False
    if tuple(a.get("tags", ())) != tuple(b.get("tags", ())):
        return False
    if "date" in a and "date" in b and a["date"] != b["date"]:
        return False
    return True


def _episode_conflict(new_name, mr, dir_se, dir_content=()):
    """集号交叉比对：库里已有同一集（哪怕文件名不同）时返回冲突描述。

    这是「追更」场景的关键判定——仅比较完整文件名会漏掉
    「库里叫 E09.mp4、新算出来叫 剧名.S01E09.mp4」这类实际重复。
    """
    # 先按内容信号判断（综艺一期分上下集时，E 号不可靠）
    new_key = content_key(new_name)
    if new_key:
        for dir_name, dir_key in dir_content:
            if _same_content(new_key, dir_key):
                return dir_name

    # 双方都没有期号/上下集等信息时，才退回按集号比对
    season, episode = _extract_se(new_name, mr)
    if episode is None:
        return None
    for dir_season, dir_episode in dir_se:
        if dir_episode != episode:
            continue
        # 任一侧缺季号时只比集号，避免因季号缺失漏判
        if season is None or dir_season is None or season == dir_season:
            return f"S{dir_season:02d}E{dir_episode:02d}" if dir_season else f"E{dir_episode:02d}"
    return None


@app.route("/ai/analyze", methods=["POST"])
def ai_analyze():
    """
    确定性分析：分享内文件 × 目标目录已有文件 → 新增 / 已在库 / 不匹配规则。
    判定口径与 quark_auto_save.dir_check_and_save 保持一致（三道闸门）。
    """
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    body = request.json or {}
    shareurl = (body.get("shareurl") or "").strip()
    savepath = (body.get("savepath") or "/").strip() or "/"
    taskname = (body.get("taskname") or "").strip() or "未命名"
    pattern = body.get("pattern") or ""
    replace = body.get("replace") or ""
    want_rule = bool(body.get("suggest_rule"))
    # 电影走完全不同的路子：没有季集，按「片名 (年份)」建子文件夹
    category = (body.get("category") or "").strip()
    if not category:
        _seg = savepath.strip("/").split("/")
        for _name in _TYPE_NAMES:
            if _name in _seg:
                category = _name
                break
    is_movie = category == "电影"

    if not shareurl:
        return jsonify({"success": False, "message": "缺少分享链接"})
    account = _require_cookie()
    if account is None:
        return jsonify({"success": False, "message": "请先在系统配置里填写 Cookie"})

    # ① 解析分享
    try:
        pwd_id, passcode, pdir_fid, _ = account.extract_url(shareurl)
        stoken_ret = account.get_stoken(pwd_id, passcode)
        if stoken_ret.get("status") != 200:
            return jsonify(
                {
                    "success": False,
                    "message": stoken_ret.get("message") or "分享链接无效或已失效",
                }
            )
        stoken = stoken_ret["data"]["stoken"]
        detail = account.get_detail(
            pwd_id, stoken, pdir_fid, _fetch_share=1, fetch_share_full_path=1
        )
        if detail.get("code") != 0:
            return jsonify(
                {"success": False, "message": detail.get("message") or "获取分享内容失败"}
            )
        share_list = detail["data"].get("list") or []
        if not share_list:
            return jsonify({"success": False, "message": "分享为空，文件可能已被删除"})
        # 分享常常是「剧名/S04/E01.mkv」这种嵌套结构，用户要选的是文件。
        # 递归展开文件夹，直接列出每一集（超过上限的文件夹仍作为一行保留）
        share_list, truncated = _expand_share_files_smart(
            account, pwd_id, stoken, pdir_fid or "", max_files=600
        )
        if not share_list:
            return jsonify({"success": False, "message": "分享为空，文件可能已被删除"})
    except Exception as e:
        return jsonify({"success": False, "message": f"解析分享失败：{e}"})

    # ①b 季号：优先信分享内容自带的季号
    share_seasons = set() if is_movie else _seasons_in_share(share_list)
    share_season, share_season_hits = _season_from_share(share_list)
    season_note = ""
    # 剧集：文件名里有 E05 这种集号但没有季标记时按第 1 季处理
    # （Emby 对单季剧同样期望 Season 01 目录；综艺不套这条，季号来自库里）
    if (not share_season and not is_movie and category in ("电视剧", "动漫")
            and not re.search(r"[Ss]\d{1,2}", savepath.rstrip("/").rsplit("/", 1)[-1] or "")):
        _has_ep = any(re.search(r"(?:^|[. _-])[Ee]\d{1,3}", (f.get("file_name") or ""))
                      for f in share_list if not f.get("dir"))
        if _has_ep:
            share_season = 1
            share_seasons = {1}

    multi_season = len(share_seasons) > 1
    if multi_season:
        # 一个链接整季打包：每季各进各的 Sxx，不能只套一个
        season_note = "分享里有 %d 季（%s），会分别入库到各自的 Sxx 子目录" % (
            len(share_seasons),
            "、".join("S%02d" % n for n in sorted(share_seasons)),
        )
    elif share_season:
        _tail = savepath.rstrip("/").rsplit("/", 1)[-1] or ""
        _cur = int(_tail[1:]) if re.fullmatch(r"[Ss]\d{1,2}", _tail) else None
        if _cur is None:
            savepath = savepath.rstrip("/") + "/S%02d" % share_season
            season_note = "按分享内容识别为第 %d 季，已自动套 S%02d 子目录" % (share_season, share_season)
        elif _cur != share_season:
            season_note = "分享内容像第 %d 季，但目标目录是 S%02d，请确认" % (share_season, _cur)

    # ② 目标目录已有文件
    dir_names = []
    savepath_exists = False
    dir_note = ""
    savepath_norm = re.sub(r"/{2,}", "/", f"/{savepath}")
    try:
        fids = account.get_fids([savepath_norm])
        if fids:
            savepath_exists = True
            ls_ret = account.ls_dir(fids[0]["fid"])
            dir_names = [
                f["file_name"] for f in (ls_ret.get("data", {}).get("list") or [])
            ]
        else:
            dir_note = f"目标目录尚不存在：{savepath_norm}（入库时会自动创建）"
    except Exception as e:
        dir_note = f"读取目标目录失败：{e}"

    # ②b 花絮素材的目标目录：S00（Special 季）
    show_root = savepath_norm
    if re.fullmatch(r"[Ss]\d{1,2}", (savepath_norm.rsplit("/", 1)[-1] or "")):
        show_root = savepath_norm.rsplit("/", 1)[0] or "/"
    extra_dir = re.sub(r"/{2,}", "/", f"{show_root}/S00")
    extra_names = []
    extra_exists = False
    extra_note = ""
    has_extra = any(
        _is_extra_file(f.get("file_name")) for f in share_list if not f.get("dir")
    )
    if has_extra:
        try:
            efids = account.get_fids([extra_dir])
            if efids:
                extra_exists = True
                els = account.ls_dir(efids[0]["fid"])
                extra_names = [
                    x["file_name"] for x in (els.get("data", {}).get("list") or [])
                ]
            else:
                extra_note = f"花絮素材会新建 {extra_dir} 统一收录"
        except Exception as e:
            extra_note = f"读取花絮目录失败：{e}"

    # ③ 逐文件判定
    mr = MagicRename(config_data.get("magic_regex", {}))
    mr.set_taskname(taskname)
    rule_pattern, rule_replace = mr.magic_regex_conv(pattern, replace)
    rule_ok = True
    if rule_pattern:
        try:
            re.compile(rule_pattern)
        except re.error:
            rule_ok = False

    # {I}/{II} 这类序号变量需要先按文件名排序赋号，否则所有文件都会拿到同一个号
    # （引擎在 do_save 里也是先 sort_file_list 再改名）
    resolved_names = {}
    if rule_ok and re.search(r"\{I+\}", rule_replace or ""):
        pending = []
        for f in share_list:
            if f.get("dir"):
                continue
            fn = f.get("file_name") or ""
            if _is_extra_file(fn):
                continue          # 花絮走 S00，不占正片的序号
            pending.append(
                {
                    "file_name_re": mr.sub(rule_pattern, rule_replace, fn),
                    "updated_at": f.get("updated_at") or 0,
                    "dir": False,
                    "_orig": fn,
                }
            )
        try:
            mr.sort_file_list(pending)
            resolved_names = {p["_orig"]: p["file_name_re"] for p in pending}
        except Exception:
            resolved_names = {}

    # 正片的集号：用 TMDB 该季的真实集号（第2期上 = S04E03）。
    # 不能按文件顺序数——分享里混着花絮和重复文件时序号会完全错位。
    _tail = savepath_norm.rsplit("/", 1)[-1] or ""
    _season_no = int(_tail[1:]) if re.fullmatch(r"[Ss]\d{1,2}", _tail) else None
    _main_files = [
        f.get("file_name") or ""
        for f in share_list
        if not f.get("dir") and not _is_extra_file(f.get("file_name"))
    ]
    season_map = {}
    if _main_files:
        try:
            season_map = tmdb.season_map_for_show(
                (config_data.get("tmdb") or {}).get("api_key")
                or os.environ.get("TMDB_API_KEY")
                or "",
                taskname,
                _main_files,
                _season_no,
            )
        except Exception:  # noqa: BLE001 - 查不到就退回顺序号
            season_map = {}

    # 花絮素材的集数：用「集数起点」把期号换算成真实集数
    # （如 S04 第1期在 TMDB 是第 138 集 → 起点填 138，则第N期 = 137+N）。
    # 留空则用期号本身，没有期号的（先导片/回顾特辑等）用顺序号。
    try:
        episode_base = int(body.get("episode_base") or 0)
    except (TypeError, ValueError):
        episode_base = 0
    if episode_base < 0:
        episode_base = 0

    # 花絮素材命名：{剧名}.S00E{集数}.{原描述}.{EXT}，按文件名排序赋号（时间顺序）
    extra_new = {}
    _extras = sorted(
        [f for f in share_list if not f.get("dir") and _is_extra_file(f.get("file_name"))],
        key=lambda f: f.get("file_name") or "",
    )
    # 序号接着 S00 里已有的最大值往下排，避免和已归档的特辑撞名
    _seq = 0
    for _n in extra_names:
        _m = re.search(r"S00E(\d{1,3})", _n, re.I)
        if _m:
            _seq = max(_seq, int(_m.group(1)))
    # 花絮的集号按 TMDB 特辑季（Season 0）匹配：先按文件名里的日期找候选，
    # 再用期号/上下集/类型词打分。这是唯一能让 Emby/Jellyfin 正确刮削的编号。
    tmdb_key = (
        (config_data.get("tmdb") or {}).get("api_key")
        or os.environ.get("TMDB_API_KEY")
        or ""
    )
    tmdb_map = {}
    if tmdb_key and _extras:
        try:
            _tail = savepath_norm.rsplit("/", 1)[-1] or ""
            _season = int(_tail[1:]) if re.fullmatch(r"[Ss]\d{1,2}", _tail) else None
            tmdb_map = tmdb.specials_for_show(
                tmdb_key,
                taskname,
                [f.get("file_name") or "" for f in _extras],
                season=_season,
            )
        except Exception:  # noqa: BLE001 - 查不到就退回期号
            tmdb_map = {}

    for _i, _f in enumerate(_extras, _seq + 1):
        _fn = _f.get("file_name") or ""
        _stem, _ext = os.path.splitext(_fn)
        _label = _extra_label(_fn, taskname)
        _hit = tmdb_map.get(_fn) or {}
        if _hit.get("number"):
            _num = int(_hit["number"])          # TMDB 特辑季的真实集号
        else:
            _num = extra_episode_number(_fn, episode_base, _i)
        extra_new[_f.get("fid")] = (
            f"{taskname}.S00E{_num:02d}.{_label}{_ext}"
            if _label
            else f"{taskname}.S00E{_num:02d}{_ext}"
        )

    # 电影：片名(年份) 建子文件夹；同一部片有多个版本时文件名带版本后缀
    movie_root = savepath_norm
    movie_counts = {}
    _folder_titles = {}
    if is_movie:
        _last = movie_root.rstrip("/").rsplit("/", 1)[-1] or ""
        if _last and _last == (taskname or "").strip():
            movie_root = movie_root.rstrip("/")[: -(len(_last) + 1)] or "/"
        for _f in share_list:
            if _f.get("dir") or _is_aux_file(_f.get("file_name") or ""):
                continue
            _d = (_f.get("dir_path") or "").strip().rstrip("/")
            _s2 = _d.rsplit("/", 1)[-1].strip() if _d else ""
            _t, _y = _movie_title_year(_s2) if _s2 else ("", "")
            _ft2, _fy2 = _movie_title_year(_f.get("file_name") or "")
            # 同一个目录里出现多个不同片名 → 这是「多部电影合辑」，
            # 不能拿目录名当片名（会把《深入敌后3》也命名成「深入敌后系列」）
            if _d and _ft2:
                _folder_titles.setdefault(_d, set()).add(_ft2)
            _t = _t or _ft2
            _y = _y or _fy2
            if _t:
                movie_counts[(_t, _y)] = movie_counts.get((_t, _y), 0) + 1

    # 一个分享里有多季时，每季各自比对各自的目录（不能拿 S04 的文件去比 S01）
    show_root = savepath_norm
    _tail0 = show_root.rstrip("/").rsplit("/", 1)[-1] or ""
    if re.fullmatch(r"[Ss]\d{1,2}", _tail0):
        show_root = show_root.rstrip("/")[: -(len(_tail0) + 1)] or "/"
    season_dir_names = {}
    if multi_season:
        for _sn in sorted(share_seasons):
            _p = "%s/S%02d" % (show_root.rstrip("/"), _sn)
            try:
                _fids = account.get_fids([_p])
                if not _fids:
                    season_dir_names[_sn] = []
                    continue
                _ls = account.ls_dir(_fids[0]["fid"])
                season_dir_names[_sn] = [
                    x.get("file_name")
                    for x in ((_ls.get("data") or {}).get("list") or [])
                ]
            except Exception:  # noqa: BLE001
                season_dir_names[_sn] = []

    # 库里已有文件的季集索引，用于识别「同一集但文件名不同」的重复
    dir_content = [(n, content_key(n)) for n in dir_names]
    dir_se = []
    for _name in dir_names:
        _s, _e = _extract_se(_name, mr)
        if _e is not None:
            dir_se.append((_s, _e))
    extra_content = [(n, content_key(n)) for n in extra_names]
    extra_se = []
    for _name in extra_names:
        _s, _e = _extract_se(_name, mr)
        if _e is not None:
            extra_se.append((_s, _e))

    rows = []
    for f in share_list:
        name = f.get("file_name") or ""
        is_dir = bool(f.get("dir"))
        row = {
            "fid": f.get("fid"),
            "file_name": name,
            "dir": is_dir,
            "size": f.get("size"),
            "updated_at": f.get("updated_at"),
            "include_items": f.get("include_items"),
            "new_name": name,
            # 文件在分享里所处的目录（展开后用于显示来源、转存时按目录分批）
            "parent_fid": f.get("parent_fid") or "",
            "dir_path": f.get("dir_path") or "",
        }
        if is_dir:
            exists = name in dir_names
            _stem, folder_season = ai.split_season(name)
            # 走到这里说明该文件夹没能展开（层级太深或文件过多）
            base_hint = (
                "分享里这一层是文件夹且未展开（层级较深或文件过多），"
                "只能整包转存，里面的文件名不能单独修改。"
            )
            if folder_season:
                status_text = "第 %d 季文件夹" % folder_season
                dir_hint = "这是分享里的第 %d 季文件夹。" % folder_season + base_hint
            else:
                status_text = "文件夹（未展开）"
                dir_hint = base_hint
            row.update(
                {
                    "status": "folder",
                    "status_text": status_text,
                    "folder_season": folder_season,
                    "dir_hint": dir_hint,
                    "in_rule": True,
                    "exists": exists,
                    "lib_name": name if exists else "",
                    "default_selected": not exists,
                    "target_savepath": savepath_norm,
                    "target_dir": "",
                }
            )
        else:
            # 海报/nfo/字幕这类辅助文件单独标记，默认不选
            is_aux = _is_aux_file(name)
            # 花絮素材（先导/纯享/花絮/加更…）统一收进 S00，不套用正片规则
            is_extra = (not is_aux) and _is_extra_file(name)
            row_season = None
            row_target = savepath_norm
            row_dir = ""
            # 文件名里已经带集号/期号的，交给规则处理，别被日期兜底抢走
            # （$TV_MAGIC 的 pattern 是空的，「规则没匹配」这个判断对它是恒真的）
            _has_ep_info = bool(re.search(
                r"[Ss]\d{1,2}[Ee]\d{1,3}"
                r"|(?:^|[. _\-])[Ee]\d{1,3}(?!\d)"
                r"|第\s*\d{1,3}\s*[期集]"
                r"|^\s*\d{1,3}\s*[期集]",       # 「09期.mp4」这种
                name,
            ))
            if is_extra:
                # 花絮素材要同时比对 S00 和正片目录：
                # 用户历史库里这类素材多半还留在 S04，不能因为换了归档位置就重下一遍。
                # 但绝不做 E 号比对——S00Exx 的 xx 是序号，和正片的 E 号无关，
                # 否则 S00E01 会被误判成库里已有的 E01。
                cmp_names = extra_names + dir_names
                cmp_content = extra_content + dir_content
                cmp_se = []
            elif is_movie:
                # 片名：分享里该文件所在的最后一层目录名（分享常按片名建目录）；
                # 年份：优先取文件名里的年份（目录名里通常没有）
                _dir = (f.get("dir_path") or "").strip().rstrip("/")
                _seg = _dir.rsplit("/", 1)[-1].strip() if _dir else ""
                _title, _year = _movie_title_year(_seg) if _seg else ("", "")
                _ft, _fy = _movie_title_year(name)
                # 该目录里有多部不同的电影 → 用文件自己的片名
                if _dir and len(_folder_titles.get(_dir) or ()) > 1 and _ft:
                    _title, _year = _ft, (_fy or _year)
                if not _title:
                    _title = _ft
                if not _year:
                    _year = _fy
                _title = _title or taskname
                _label = ("%s (%s)" % (_title, _year)) if _year else _title
                _ver = _movie_version(name) if movie_counts.get((_title, _year), 0) > 1 else ""
                _ext = os.path.splitext(name)[1]
                row["movie_title"] = _title
                row["movie_year"] = _year
                row["movie_version"] = _ver
                row["_movie_name"] = ("%s - %s%s" % (_label, _ver, _ext)) if _ver else ("%s%s" % (_label, _ext))
                row_target = "%s/%s" % (movie_root.rstrip("/"), _label)
                row_dir = _label
                cmp_names = []
                cmp_content = []
                cmp_se = []
            else:
                # 一个链接整季打包时，按文件自己所属的季分别入库
                row_season = _season_of_path(f.get("dir_path")) or _season_of_text(name)
                # 不在任何季目录里的（分享根目录混着的几期）：
                # 用播出日期问 TMDB 该进第几季，避免落到剧名根目录下
                if not row_season and not is_movie and tmdb_key:
                    _d0 = _air_date(name, f.get("dir_path"))
                    if _d0:
                        try:
                            row_season = tmdb.season_for_date(tmdb_key, taskname, _d0)
                        except Exception:  # noqa: BLE001
                            row_season = None
                        if row_season:
                            row["season_by_date"] = _d0
                if multi_season and row_season:
                    _names = season_dir_names.get(row_season) or []
                    cmp_names = _names
                    cmp_content = [(n, content_key(n)) for n in _names]
                    cmp_se = [
                        (s2, e2)
                        for (s2, e2) in (_extract_se(n, mr) for n in _names)
                        if e2 is not None
                    ]
                    row_target = "%s/S%02d" % (show_root.rstrip("/"), row_season)
                    row_dir = "S%02d" % row_season
                else:
                    cmp_names = dir_names
                    cmp_content = dir_content
                    cmp_se = dir_se
            row["is_extra"] = is_extra
            row["aux"] = is_aux
            # 目标季号：多季分享用该文件自己的季，否则用入库路径上的 Sxx
            _season_label = ""
            if row_dir:
                _season_label = row_dir
            else:
                _t2 = savepath_norm.rstrip("/").rsplit("/", 1)[-1] or ""
                if re.fullmatch(r"[Ss]\d{1,2}", _t2):
                    _season_label = _t2.upper()
                elif share_season:
                    _season_label = "S%02d" % share_season
            row["season_label"] = _season_label
            if is_extra:
                _hit = tmdb_map.get(name) or {}
                if _hit.get("number"):
                    row["tmdb_episode"] = _hit.get("number")
                    row["tmdb_match"] = _hit.get("by") or ""
                    row["tmdb_name"] = _hit.get("name") or ""
            row["target_savepath"] = extra_dir if is_extra else row_target
            row["target_dir"] = "S00" if is_extra else row_dir
            if is_extra:
                in_rule = True
                new_name = extra_new.get(f.get("fid")) or name
            elif is_aux:
                in_rule = False          # 辅助文件不套正片规则
                new_name = name
            elif (not is_extra and not is_movie and not _has_ep_info
                  and _air_date(name, f.get("dir_path"))     # 真能算出日期才走兜底
                  and not (rule_pattern and rule_ok and re.search(rule_pattern, name))):
                # 规则匹配不上时，若文件名是日期（12.08.mp4 / 20250830.xxx.mp4），
                # 用「剧名.播出日期」命名——Emby 会按播出日期匹配集数，
                # 总比留着 12.08.mp4 这种名字刮削不出来强
                _air = _air_date(name, f.get("dir_path"))
                if _air:
                    _ep = None
                    if tmdb_key and row_season:
                        try:
                            _ep = tmdb.episode_for_date(tmdb_key, taskname, row_season, _air)
                        except Exception:  # noqa: BLE001
                            _ep = None
                    _ext2 = os.path.splitext(name)[1]
                    # 剧集要带 Sxx（S01E05 才是 Emby 认的写法），综艺按用户模板不带
                    _sw = row.get("season_label") or ""
                    _is_tv = category in ("电视剧", "动漫") and _sw
                    if _ep:
                        if _is_tv:
                            new_name = "%s.%sE%02d.%s%s" % (taskname, _sw, _ep, _air, _ext2)
                        else:
                            new_name = "%s.E%02d.%s%s" % (taskname, _ep, _air, _ext2)
                        row["tmdb_episode"] = _ep
                    else:
                        if _is_tv:
                            new_name = "%s.%s.%s%s" % (taskname, _sw, _air, _ext2)
                        else:
                            new_name = "%s.%s%s" % (taskname, _air, _ext2)
                    in_rule = True
                    row["date_named"] = True
                    row["air_date"] = _air
                else:
                    # 既匹配不上规则、又没日期 → 保留原名（否则 new_name 未定义会 500）
                    in_rule = False
                    new_name = name
            elif is_movie:
                in_rule = True
                new_name = row.get("_movie_name") or name
            else:
                in_rule = (
                    bool(re.search(rule_pattern, name))
                    if (rule_pattern and rule_ok)
                    else True
                )
                _tpl = rule_replace or ""
                _num = season_map.get(name)
                # 综艺的「第N期」就是集号：用 N，别用全局文件序号
                _period = re.search(r"第\s*(\d{1,3})\s*期", name)
                if _period and re.search(r"\{I+\}", _tpl) and not _num:
                    _num = int(_period.group(1))
                if _num and re.search(r"\{I+\}", _tpl):
                    # 把模板里的 {II} 换成 TMDB 的真实集号（补齐两位，和库里一致）
                    _tpl = re.sub(r"\{I+\}", str(_num).zfill(2), _tpl)
                    row["tmdb_episode"] = _num
                # {SXX} 用目标目录的季号：综艺文件名里没有季标记，
                # 让 MagicRename 从文件名猜会退回 S01（用户踩过这个坑）
                if _season_label and re.search(r"\{SXX\}", _tpl):
                    _tpl = _tpl.replace("{SXX}", _season_label)
                # sort_file_list 的 {II} 是「全部文件的全局序号」，
                # 多季分享里会变成 E118 这种；只有拿不到真实集号时才用它
                # 规则不匹配、但文件名里有集号（如「09期.mp4」）→ 按集号命名，
                # 否则用户只会看到「不匹配规则」、一个 E 号都没有
                if not in_rule:
                    _ep2 = _episode_from_name(name)
                    if _ep2:
                        _ext3 = os.path.splitext(name)[1]
                        _sw2 = row.get("season_label") or ""
                        if category in ("电视剧", "动漫") and _sw2:
                            new_name = "%s.%sE%02d%s" % (taskname, _sw2, _ep2, _ext3)
                        else:
                            new_name = "%s.E%02d.第%d期%s" % (taskname, _ep2, _ep2, _ext3)
                        in_rule = True
                        row["ep_from_name"] = _ep2
                        row["status_text"] = "可新增（按集号命名）"
                _has_seq = bool(re.search(r"\{I+\}", rule_replace or ""))
                if row.get("ep_from_name"):
                    pass          # 名字已经按集号算好了，别再覆盖
                elif _has_seq and _num is None:
                    new_name = resolved_names.get(name) or (
                        mr.sub(rule_pattern, _tpl, name) if rule_ok else name
                    )
                else:
                    new_name = mr.sub(rule_pattern, _tpl, name) if rule_ok else name
            # 集号补零：S02E6 → S02E06（和库里其它文件保持一致）
            if new_name and not is_movie:
                new_name = re.sub(r"([Ss]\d\d[Ee])(\d)(?!\d)", r"\g<1>0\2", new_name)
            exists_orig = bool(mr.is_exists(name, cmp_names))
            exists_new = bool(mr.is_exists(new_name, cmp_names))
            row["new_name"] = new_name or name
            row["in_rule"] = in_rule
            if rule_ok and re.search(
                r"\{(TASKNAME|SXX|S|E|I+|EXT|PART|DATE|YEAR|CHINESE|VER)\}", new_name or ""
            ):
                row["unresolved"] = True
                row.update({"status": "rule_error", "status_text": "命名变量未解析"})
            elif rule_ok and re.search(r"[Ss]\d\d[Ee](?!\d)", new_name or ""):
                # 集号解析出空值（如「S01E.mkv」），说明源文件名里没有集号
                row["unresolved"] = True
                row.update({"status": "rule_error", "status_text": "集号解析失败"})
            row["exists"] = exists_orig or exists_new
            if not rule_ok:
                row.update({"status": "rule_error", "status_text": "规则无效"})
            elif exists_orig:
                row.update({"status": "exists", "status_text": "已在库（同名）",
                            "lib_name": name})
            elif exists_new:
                row.update({"status": "exists", "status_text": "已在库（改名后同名）",
                            "lib_name": new_name})
            elif not in_rule:
                row.update({"status": "unmatched", "status_text": "不匹配规则"})
            else:
                conflict = _episode_conflict(row["new_name"], mr, cmp_se, cmp_content)
                if conflict:
                    row.update(
                        {
                            "status": "dup_episode",
                            "status_text": f"疑似重复（库里已有 {conflict}）",
                            "conflict": conflict,
                        }
                    )
                else:
                    row.update({"status": "new", "status_text": "可新增"})
            row["default_selected"] = row["status"] == "new"
        rows.append(row)

    # 同一集有多个版本（1080p / 4K / 4K高码 / 60fps…）时，把版本标记补进文件名。
    # 这类分享很常见：一集同时压了普通版和 4K 版，规则重命名后名字完全一样，
    # 不处理就会互相覆盖。
    def _version_tag(text):
        tags = []
        for pat, label in (
            (r"2160p|\b4k\b", "4K"),
            (r"1080p", "1080p"),
            (r"720p", "720p"),
            (r"高码", "高码"),
            (r"60fps", "60fps"),
            (r"hdr", "HDR"),
            (r"\bdv\b|dolby", "DV"),
        ):
            if re.search(pat, str(text or ""), re.I):
                tags.append(label)
        return "".join(tags)

    _groups = {}
    for _r in rows:
        if _r.get("dir") or _r.get("aux") or _r.get("movie_title"):
            continue      # 电影的文件名里已经带版本了，不再叠加
        _nm = (_r.get("new_name") or "").strip()
        if _nm:
            _groups.setdefault(((_r.get("target_savepath") or ""), _nm), []).append(_r)
    for _key, _grp in _groups.items():
        if len(_grp) < 2:
            continue
        _tags = [_version_tag(_r.get("file_name")) or "原版" for _r in _grp]
        # 同集同版本的多份 = 重复文件：只留最大的一份，其余标记为重复
        _by_tag = {}
        for _r, _tag in zip(_grp, _tags):
            _by_tag.setdefault(_tag, []).append(_r)
        for _tag, _same in _by_tag.items():
            if len(_same) < 2:
                continue
            _same.sort(key=lambda r: -(r.get("size") or 0))
            for _dup in _same[1:]:
                _dup["dup_copy"] = True
                _dup["default_selected"] = False
                _dup["status"] = "dup_copy"
                _dup["status_text"] = "重复文件（同集同版本，已留一份）"
        # 只有版本不同才需要改名区分
        _keep = [r for r in _grp if not r.get("dup_copy")]
        _keep_tags = [_version_tag(r.get("file_name")) or "原版" for r in _keep]
        if len(_keep) < 2 or len(set(_keep_tags)) != len(_keep_tags):
            continue
        for _r, _tag in zip(_keep, _keep_tags):
            _stem, _ext = os.path.splitext(_r["new_name"])
            _r["new_name"] = "%s.%s%s" % (_stem, _tag, _ext)
            _r["version_tag"] = _tag

    # 命名撞车检测：多个文件算出同一个名字，转存时会互相覆盖，
    # 这种情况通常说明所选规则把「第N期上/下」这类区分信息丢了
    # 只有「同一个目标目录 + 同一个文件名」才会真覆盖；
    # 辅助文件（海报/nfo）本来就不入库，也不参与判断
    name_count = {}
    for r in rows:
        if r.get("dir") or r.get("aux") or r.get("dup_copy"):
            continue
        nm = (r.get("new_name") or "").strip()
        if nm:
            key = ((r.get("target_savepath") or ""), nm)
            name_count[key] = name_count.get(key, 0) + 1
    for r in rows:
        if r.get("dir") or r.get("aux") or r.get("dup_copy"):
            continue
        nm = (r.get("new_name") or "").strip()
        if nm and name_count.get(((r.get("target_savepath") or ""), nm), 0) > 1:
            r["collision"] = True
            r["default_selected"] = False
            if r.get("status") in ("new", "dup_episode", "unmatched"):
                r["status_text"] = r["status_text"] + "（与其他文件重名）"

    # 按集号兜底命名的行，状态文案单独标出来（透明一些）
    for _r in rows:
        if _r.get("ep_from_name") and _r.get("status") == "new":
            _r["status_text"] = "可新增（按集号命名）"

    # 海报/nfo 这类辅助文件单独标一下，别混在「不匹配规则」里
    for _r in rows:
        if _r.get("aux") and _r.get("status") == "unmatched":
            _r["status_text"] = "辅助文件（海报/nfo）"
            _r["default_selected"] = False

    # 排序：先按状态分组（能直接入库的排前面，「不匹配规则」「已在库」放后面），
    # 组内按文件名里的日期倒序——最新的集数排最前面，方便直接勾新出的那几集
    _status_order = {"new": 0, "dup_episode": 1, "folder": 2, "unmatched": 3,
                     "exists": 4, "rule_error": 5}

    def _date_key(row):
        """文件名里的日期（YYYYMMDD 整数），没有则 0"""
        text = tmdb.name_date(row.get("file_name") or "")
        return int(text.replace("-", "")) if text else 0

    for _r in rows:
        _r["date_key"] = _date_key(_r)
    rows.sort(
        key=lambda r: (
            _status_order.get(r.get("status"), 9),
            -r["date_key"],                      # 新的在前
            -(r.get("updated_at") or 0),          # 同日期按文件时间兜底
        )
    )

    stats = {
        "total": len(rows),
        "new": sum(1 for r in rows if r["status"] == "new"),
        "exists": sum(1 for r in rows if r["status"] == "exists"),
        "dup_episode": sum(1 for r in rows if r["status"] == "dup_episode"),
        "unmatched": sum(1 for r in rows if r["status"] == "unmatched" and not r.get("aux")),
        "aux": sum(1 for r in rows if r.get("aux")),
        "dup_copy": sum(1 for r in rows if r.get("dup_copy")),
        "folder": sum(1 for r in rows if r["status"] == "folder"),
        "collision": sum(1 for r in rows if r.get("collision")),
        "extra": sum(1 for r in rows if r.get("is_extra")),
    }

    data = {
        "rows": rows,
        "stats": stats,
        "savepath": savepath_norm,
        "savepath_exists": savepath_exists,
        # 分享内容里识别出的季号（自动套 Sxx 用），以及给用户的说明
        "movie_mode": is_movie,
        "movie_root": movie_root if is_movie else "",
        "season_from_share": share_season,
        "share_seasons": sorted(share_seasons),
        "multi_season": multi_season,
        "season_from_share_hits": share_season_hits,
        "season_note": season_note,
        "dir_note": dir_note,
        "extra_dir": extra_dir,
        "episode_base": episode_base,
        "extra_exists": extra_exists,
        "extra_note": extra_note,
        "tmdb_matched": len(tmdb_map),
        "tmdb_main_matched": len(season_map),
        "tmdb_enabled": bool(tmdb_key),
        "dir_file_count": len(dir_names),
        "dir_files": dir_names[:100],
        # 分享里的文件夹已递归展开成文件；被截断时提示一下
        "truncated": truncated,
        "expanded_count": sum(1 for r in rows if r.get("dir_path")),
        "rule": {
            "pattern": pattern,
            "replace": replace,
            "resolved_pattern": rule_pattern,
            "resolved_replace": rule_replace,
            "valid": rule_ok,
        },
    }

    # ④ 可选：推荐命名规则
    # 只在用户点了「推荐命名规则」时才调 AI：
    # 撞名时自动推荐会让每次分析都多等一次 AI 调用（实测 +10 秒），
    # 改为在撞名告警里引导用户点按钮
    if want_rule:
        try:
            filenames = [r["file_name"] for r in rows if not r["dir"]]
            data["rule_suggestion"] = ai.suggest_rename_rule(
                taskname,
                filenames,
                config_data.get("magic_regex", {}),
                mr.sub,
                _ai_client(),
            )
        except Exception as e:
            data["rule_suggestion"] = {"error": str(e)}

    return jsonify({"success": True, "data": data})


def _share_latest(account, shareurl, max_files=120):
    """
    读某个分享里最新的几个文件。

    搜索结果的标题只反映「发布时的描述」，判断不出这个分享现在更新到哪一集，
    合集类分享（标题写「第三季」但里面是全季）尤其容易误判。
    返回 {"total": n, "latest": [{"file_name", "dir_path", "date"}], "truncated": bool}
    """
    pwd_id, passcode, pdir_fid, _ = account.extract_url(shareurl)
    if not pwd_id:
        raise ValueError("分享链接无效")
    stoken_ret = account.get_stoken(pwd_id, passcode)
    if stoken_ret.get("status") != 200:
        raise ValueError(stoken_ret.get("message") or "分享不可用")
    stoken = stoken_ret["data"]["stoken"]
    # 必须先带 _fetch_share=1 拉一次，后续 get_detail 才拿得到列表
    account.get_detail(pwd_id, stoken, pdir_fid or "", _fetch_share=1, fetch_share_full_path=1)
    items, truncated = _expand_share_files_smart(
        account, pwd_id, stoken, pdir_fid or "", max_files=max_files
    )
    files = [i for i in items if not i.get("dir")]
    for f in files:
        f["_date"] = tmdb.name_date(f.get("file_name") or "")
    files.sort(key=lambda f: (f["_date"], f.get("updated_at") or 0), reverse=True)
    return {
        "total": len(files),
        "truncated": truncated,
        "latest": [
            {
                "file_name": f.get("file_name"),
                "dir_path": f.get("dir_path") or "",
                "date": f["_date"],
            }
            for f in files[:5]
        ],
    }


@app.route("/ai/share_latest", methods=["POST"])
def ai_share_latest():
    """看某个分享里最新的几个文件（点之前就知道它更新到哪一集）"""
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    body = request.json or {}
    shareurl = (body.get("shareurl") or "").strip()
    if not shareurl:
        return jsonify({"success": False, "message": "缺少分享链接"})
    account = _require_cookie()
    if account is None:
        return jsonify({"success": False, "message": "请先在系统配置里填写 Cookie"})
    try:
        return jsonify({"success": True, "data": _share_latest(account, shareurl)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "message": f"读取分享失败：{e}"})


@app.route("/ai/delete", methods=["POST"])
def ai_delete():
    """
    删除目标目录里已存在的文件（想重新导入更好的版本时用）。
    走回收站（引擎的 delete 用 action_type=2），删错了可以在网盘回收站恢复。
    """
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    body = request.json or {}
    savepath = (body.get("savepath") or "/").strip() or "/"
    names = [str(n).strip() for n in (body.get("file_names") or []) if str(n).strip()]
    if not names:
        return jsonify({"success": False, "message": "没有指定要删除的文件"})
    account = _require_cookie()
    if account is None:
        return jsonify({"success": False, "message": "请先在系统配置里填写 Cookie"})

    savepath_norm = re.sub(r"/{2,}", "/", f"/{savepath}")
    try:
        fids = account.get_fids([savepath_norm])
        if not fids:
            return jsonify({"success": False, "message": f"目录不存在：{savepath_norm}"})
        items = account.ls_dir(fids[0]["fid"]).get("data", {}).get("list") or []
    except Exception as e:
        return jsonify({"success": False, "message": f"读取目录失败：{e}"})

    want = set(names)
    targets = [it for it in items if (it.get("file_name") or "") in want]
    found = {it.get("file_name") for it in targets}
    if not targets:
        return jsonify({"success": False, "message": "目录里没有找到要删除的文件（可能已被删或已改名）"})

    try:
        ret = account.delete([it["fid"] for it in targets])
        ok = ret.get("code") == 0
    except Exception as e:
        return jsonify({"success": False, "message": f"删除失败：{e}"})

    return jsonify(
        {
            "success": True,
            "data": {
                "ok": ok,
                "deleted": sorted(found),
                "missing": sorted(want - found),
                "message": ret.get("message") or "",
            },
        }
    )


@app.route("/ai/commit", methods=["POST"])
def ai_commit():
    """
    一次性转存：按界面确认的最终文件名重命名入库。

    不创建任务；直接调用 do_save_task，因此不经过 do_save，
    天然绕过 runweek 周期限制（is_time 只在 do_save 中调用）。
    """
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    body = request.json or {}
    shareurl = (body.get("shareurl") or "").strip()
    savepath = (body.get("savepath") or "/").strip() or "/"
    taskname = (body.get("taskname") or "").strip() or "AI 追剧"
    files = body.get("files") or []

    if not shareurl:
        return jsonify({"success": False, "message": "缺少分享链接"})
    if not files:
        return jsonify({"success": False, "message": "没有勾选任何文件"})
    account = _require_cookie()
    if account is None:
        return jsonify({"success": False, "message": "请先在系统配置里填写 Cookie"})

    # 勾选的条目可能来自分享里的不同目录（文件夹已展开成文件），
    # 转存引擎一次只处理一个目录，所以按目录分批转存
    # 花絮素材会落到 S00，所以分组键要带上目标路径
    groups = {}
    for f in files:
        if not f.get("fid"):
            continue
        key = (
            f.get("parent_fid") or "",
            re.sub(r"/{2,}", "/", f"/{(f.get('target_savepath') or '').strip() or savepath}"),
        )
        groups.setdefault(key, []).append(f)
    if not groups:
        return jsonify({"success": False, "message": "勾选的条目缺少 fid"})

    savepath_norm = re.sub(r"/{2,}", "/", f"/{savepath}")
    # 目标目录（含 S00）不存在则创建
    for _dir in {sp for (_pf, sp) in groups}:
        try:
            if not account.get_fids([_dir]):
                account.mkdir(_dir)
        except Exception:
            pass

    try:
        pwd_id, _passcode, root_pdir, _ = account.extract_url(shareurl)
    except Exception:
        pwd_id, root_pdir = None, ""

    import contextlib
    import io

    # 复用引擎的通知机制：do_save_task 内部会 add_notify 累积消息，
    # 这里清空后统一读取，再走项目已有的推送渠道
    import quark_auto_save as qas

    qas.NOTIFYS.clear()

    buf = io.StringIO()
    ok_all = True
    renamed = 0
    last_tree = None
    last_savepath = ""
    # 本次入库实际写过的目标目录（S04、S00 各一个），插件要按目录逐个触发
    saved_paths = []
    for (parent_fid, group_savepath), group in groups.items():
        rename_map = {}
        for f in group:
            orig = f.get("file_name") or ""
            if orig:
                rename_map[orig] = (f.get("new_name") or "").strip()
        renamed += len(rename_map)

        # 分享根目录用原链接；子目录用 #/list/share/<fid> 定位
        sub_url = shareurl
        if parent_fid and str(parent_fid) != str(root_pdir or ""):
            sub_url = "https://pan.quark.cn/s/%s#/list/share/%s" % (pwd_id, parent_fid)

        task = {
            "taskname": taskname,
            "shareurl": sub_url,
            "savepath": group_savepath,
            # 成员资格由用户勾选（fid_list）决定；pattern 放开为全匹配，
            # 文件名一律由 rename_map 给出，避免规则再次筛掉用户选中的文件
            "pattern": ".*",
            "replace": "",
            "fid_list": [f["fid"] for f in group],
            "rename_map": rename_map,
        }
        buf.write(
            "—— %s → %s（%d 个文件）\n"
            % (parent_fid or "分享根目录", group_savepath, len(group))
        )
        try:
            with contextlib.redirect_stdout(buf):
                tree = account.do_save_task(task)
            ok_all = ok_all and bool(tree)
            if tree:
                last_tree = tree
                last_savepath = group_savepath
                if group_savepath not in saved_paths:
                    saved_paths.append(group_savepath)
        except Exception as e:
            buf.write("转存异常：%s\n%s\n" % (e, traceback.format_exc()))
            ok_all = False

    # 入库成功后触发插件：与定时任务的 do_save 保持一致
    # （SmartStrm 生成 .strm、alist/emby 刷新等都在插件里）
    if ok_all:
        try:
            plugins, _pc, task_plugins_cfg = Config.load_plugins(
                dict(config_data.get("plugins") or {})
            )
            # 花絮会落到 S00、正片落到 S04，SmartStrm 一次只扫一个目录，
            # 所以每个实际写过的目录都要单独触发一次，否则另一个目录不会生成 strm
            targets = saved_paths or [savepath_norm]
            triggered = _run_plugins(
                plugins,
                targets,
                {
                    "taskname": taskname,
                    "shareurl": shareurl,
                    "pattern": ".*",
                    "replace": "",
                    "addition": task_plugins_cfg,
                },
                account,
                last_tree,
                buf,
            )
            if triggered:
                buf.write(
                    "\n🧩 已触发插件：%s（覆盖目录 %s）\n"
                    % ("、".join(triggered), "、".join(targets))
                )
            else:
                buf.write("\n（没有启用中的插件需要触发）\n")
        except Exception as e:
            buf.write("\n插件加载异常：%s\n" % e)

    # 入库后推送通知：复用项目已有的渠道（notify 模块 + 配置里的 push_config）
    notified = False
    if qas.NOTIFYS:
        body = "（AI 追剧手动入库）\n" + "\n".join(qas.NOTIFYS)
        try:
            # send_ql_notify 从模块级 CONFIG_DATA 读 push_config，
            # Web 进程里它一直是空的，这里临时指向 Web 的配置
            prev_cfg = getattr(qas, "CONFIG_DATA", None)
            qas.CONFIG_DATA = config_data
            try:
                with contextlib.redirect_stdout(buf):
                    qas.send_ql_notify("【夸克自动转存】", body)
                notified = True
                buf.write("\n📮 已通过已配置的通知渠道推送\n")
            finally:
                qas.CONFIG_DATA = prev_cfg
        except Exception as e:
            buf.write("\n通知推送异常：%s\n" % e)

    return jsonify(
        {
            "success": True,
            "data": {
                "ok": ok_all,
                "notified": notified,
                "renamed": renamed,
                "batches": len(groups),
                "log": buf.getvalue().strip()[-6000:],
            },
        }
    )


@app.route("/get_share_detail", methods=["POST"])
def get_share_detail():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    shareurl = request.json.get("shareurl", "")
    stoken = request.json.get("stoken", "")
    account = Quark()
    pwd_id, passcode, pdir_fid, paths = account.extract_url(shareurl)
    if not stoken:
        get_stoken = account.get_stoken(pwd_id, passcode)
        if get_stoken.get("status") == 200:
            stoken = get_stoken["data"]["stoken"]
        else:
            return jsonify(
                {"success": False, "data": {"error": get_stoken.get("message")}}
            )
    share_detail = account.get_detail(
        pwd_id, stoken, pdir_fid, _fetch_share=1, fetch_share_full_path=1
    )

    if share_detail.get("code") != 0:
        return jsonify(
            {"success": False, "data": {"error": share_detail.get("message")}}
        )

    data = share_detail["data"]
    data["paths"] = [
        {"fid": i["fid"], "name": i["file_name"]}
        for i in share_detail["data"].get("full_path", [])
    ] or paths
    data["stoken"] = stoken

    # 过滤 01x.mp4 类型无效视频格式
    if os.getenv("FILTER_INVALID_VIDEO", "true") == "true":
        for share_file in data["list"]:
            if (
                share_file["file_name"].lower().endswith((".mp4", ".mkv"))
                and not share_file["dir"]
                and share_file["obj_category"] != "video"
            ):
                return jsonify({"success": False, "data": {"error": "无效视频格式"}})

    # 正则处理预览
    def preview_regex(data):
        task = request.json.get("task", {})
        magic_regex = request.json.get("magic_regex", {})
        mr = MagicRename(magic_regex)
        mr.set_taskname(task.get("taskname", ""))
        account = Quark(config_data["cookie"][0])
        get_fids = account.get_fids([task.get("savepath", "")])
        if get_fids:
            dir_file_list = account.ls_dir(get_fids[0]["fid"])["data"]["list"]
            dir_filename_list = [dir_file["file_name"] for dir_file in dir_file_list]
        else:
            dir_file_list = []
            dir_filename_list = []

        pattern, replace = mr.magic_regex_conv(
            task.get("pattern", ""), task.get("replace", "")
        )
        for share_file in data["list"]:
            search_pattern = (
                task["update_subdir"]
                if share_file["dir"] and task.get("update_subdir")
                else pattern
            )
            if re.search(search_pattern, share_file["file_name"]):
                # 文件名重命名，目录不重命名
                file_name_re = (
                    share_file["file_name"]
                    if share_file["dir"]
                    else mr.sub(pattern, replace, share_file["file_name"])
                )
                if file_name_saved := mr.is_exists(
                    file_name_re,
                    dir_filename_list,
                    (task.get("ignore_extension") and not share_file["dir"]),
                ):
                    share_file["file_name_saved"] = file_name_saved
                else:
                    share_file["file_name_re"] = file_name_re

        # 文件列表排序
        if re.search(r"\{I+\}", replace):
            mr.set_dir_file_list(dir_file_list, replace)
            mr.sort_file_list(data["list"])

    if request.json.get("task"):
        preview_regex(data)

    return jsonify({"success": True, "data": data})


@app.route("/get_savepath_detail")
def get_savepath_detail():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        if fid := request.args.get("fid", None):
            file_list = _get_file_list(fid=fid)
        elif path := request.args.get("path", "/"):
            file_list = _get_file_list(path=path)
        return jsonify({"success": True, "data": file_list})
    except Exception as e:
        return jsonify({"success": False, "data": {"error": str(e)}})


def _get_file_list(fid: str = None, path: str = None):
    account = Quark(config_data["cookie"][0])
    paths = []
    if path and not fid:
        path = re.sub(r"/+", "/", path)
        if path == "/":
            fid = 0
        else:
            dir_names = path.split("/")
            if dir_names[0] == "":
                dir_names.pop(0)
            path_fids = []
            current_path = ""
            for dir_name in dir_names:
                current_path += "/" + dir_name
                path_fids.append(current_path)
            if get_fids := account.get_fids(path_fids):
                fid = get_fids[-1]["fid"]
                paths = [
                    {"fid": get_fid["fid"], "name": dir_name}
                    for get_fid, dir_name in zip(get_fids, dir_names)
                ]
            else:
                raise FileNotFoundError("获取fid失败")
    file_list = {
        "fid": fid,
        "list": account.ls_dir(fid)["data"]["list"],
        "paths": paths,
    }
    return file_list


def _path_to_fid(path):
    """根据路径获取文件的fid"""
    if not path:
        raise ValueError("路径不能为空")
    path = re.sub(r"/+", "/", path)
    if path == "/":
        return 0
    file_list = _get_file_list(None, os.path.dirname(path))
    for file in file_list["list"]:
        if file["file_name"] == os.path.basename(path):
            return file["fid"]
    raise FileNotFoundError(f"未找到文件: {path}")


@app.route("/delete_file", methods=["POST"])
def delete_file():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        fid = request.json.get("fid") or _path_to_fid(request.json.get("path"))
        if fid:
            account = Quark(config_data["cookie"][0])
            response = account.delete([fid])
            response["success"] = response["code"] == 0
            return jsonify(response)
        else:
            raise ValueError("缺失必要字段: fid 或 path")
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/rename_file", methods=["POST"])
def rename_file():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        fid = request.json.get("fid") or _path_to_fid(request.json.get("path"))
        file_name = request.json.get("file_name")
        if fid and file_name:
            account = Quark(config_data["cookie"][0])
            response = account.rename(fid, file_name)
            response["success"] = response["code"] == 0
            return jsonify(response)
        else:
            raise ValueError("缺失必要字段: fid, file_name")
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# 添加任务接口
@app.route("/api/add_task", methods=["POST"])
def add_task():
    global config_data
    # 验证token
    if not is_login():
        return jsonify({"success": False, "code": 1, "message": "未登录"}), 401
    # 必选字段
    request_data = request.json
    required_fields = ["taskname", "shareurl", "savepath"]
    for field in required_fields:
        if field not in request_data or not request_data[field]:
            return (
                jsonify(
                    {"success": False, "code": 2, "message": f"缺少必要字段: {field}"}
                ),
                400,
            )
    if not request_data.get("addition"):
        request_data["addition"] = task_plugins_config_default
    # 添加任务
    config_data["tasklist"].append(request_data)
    Config.write_json(CONFIG_PATH, config_data)
    logging.info(f">>> 通过API添加任务: {request_data['taskname']}")
    return jsonify(
        {"success": True, "code": 0, "message": "任务添加成功", "data": request_data}
    )


# 定时任务执行的函数
def run_python(args):
    logging.info(f">>> 定时运行任务")
    try:
        result = subprocess.run(
            f"{PYTHON_PATH} {args}",
            shell=True,
            timeout=TASK_TIMEOUT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # 输出执行日志
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    logging.info(line)

        if result.returncode == 0:
            logging.info(f">>> 任务执行成功")
        else:
            logging.error(f">>> 任务执行失败，返回码: {result.returncode}")
            if result.stderr:
                logging.error(f"错误信息: {result.stderr[:500]}")
    except subprocess.TimeoutExpired as e:
        logging.error(f">>> 任务执行超时(>{TASK_TIMEOUT}s)，强制终止")
    except Exception as e:
        logging.error(f">>> 任务执行异常: {str(e)}")
        logging.error(traceback.format_exc())
    finally:
        # 确保函数能够正常返回
        logging.debug(f">>> run_python 函数执行完成")


# 重新加载任务
def reload_tasks():
    # 读取定时规则
    if crontab := config_data.get("crontab"):
        if scheduler.state == 1:
            scheduler.pause()  # 暂停调度器
        trigger = CronTrigger.from_crontab(crontab)
        scheduler.remove_all_jobs()
        scheduler.add_job(
            run_python,
            trigger=trigger,
            args=[f"{SCRIPT_PATH} {CONFIG_PATH}"],
            id=SCRIPT_PATH,
            max_instances=1,  # 最多允许1个实例运行
            coalesce=True,  # 合并错过的任务，避免堆积
            misfire_grace_time=300,  # 错过任务的宽限期(秒)，超过则跳过
            replace_existing=True,  # 替换已存在的同ID任务
        )
        if scheduler.state == 0:
            scheduler.start()
        elif scheduler.state == 2:
            scheduler.resume()
        scheduler_state_map = {0: "停止", 1: "运行", 2: "暂停"}
        logging.info(">>> 重载调度器")
        logging.info(f"调度状态: {scheduler_state_map[scheduler.state]}")
        logging.info(f"定时规则: {crontab}")
        logging.info(f"现有任务: {scheduler.get_jobs()}")
        return True
    else:
        logging.info(">>> no crontab")
        return False


def init():
    global config_data, task_plugins_config_default
    logging.info(">>> 初始化配置")
    # 检查配置文件是否存在
    if not os.path.exists(CONFIG_PATH):
        if not os.path.exists(os.path.dirname(CONFIG_PATH)):
            os.makedirs(os.path.dirname(CONFIG_PATH))
        with open("quark_config.json", "rb") as src, open(CONFIG_PATH, "wb") as dest:
            dest.write(src.read())

    # 读取配置
    config_data = Config.read_json(CONFIG_PATH)
    Config.breaking_change_update(config_data)
    if not config_data.get("magic_regex"):
        config_data["magic_regex"] = MagicRename().magic_regex

    # 默认管理账号
    config_data["webui"] = {
        "username": os.environ.get("WEBUI_USERNAME")
        or config_data.get("webui", {}).get("username", "admin"),
        "password": os.environ.get("WEBUI_PASSWORD")
        or config_data.get("webui", {}).get("password", "admin123"),
    }

    # 默认定时规则
    if not config_data.get("crontab"):
        config_data["crontab"] = "0 8,18,20 * * *"

    # 初始化插件配置
    _, plugins_config_default, task_plugins_config_default = Config.load_plugins()
    for name, config in plugins_config_default.items():
        for key, value in config.items():
            config[key] = (
                config_data.setdefault("plugins", {})
                .setdefault(name, {})
                .get(key, value)
            )
    config_data["plugins"] = plugins_config_default

    # 更新配置
    Config.write_json(CONFIG_PATH, config_data)


if __name__ == "__main__":
    init()
    reload_tasks()
    logging.info(">>> 启动Web服务")
    logging.info(f"运行在: http://{HOST}:{PORT}")
    app.run(
        debug=DEBUG,
        host=HOST,
        port=PORT,
    )
