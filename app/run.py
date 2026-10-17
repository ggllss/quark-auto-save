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
import ai
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

    def ps_search():
        if ps_data.get("server"):
            return PanSou(ps_data.get("server")).search(query, deep) or []
        return []

    search_results = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        if str(cs_data.get("enable", "true")).lower() == "true":
            futures.append(executor.submit(cs_search))
        if str(ps_data.get("enable", "true")).lower() == "true":
            futures.append(executor.submit(ps_search))
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
    intent = ai.parse_intent(keyword, client)
    queries = [(q or "").lower() for q in (intent.get("queries") or [])[:3] if q]

    def gather():
        pool, seen = [], set()
        for query in queries:
            try:
                for item in _collect_suggestions(query, False):
                    url = item.get("shareurl") or ""
                    if url and url not in seen:
                        seen.add(url)
                        pool.append(item)
            except Exception:
                continue
        return pool

    pool = gather()
    retried = False
    # 公共搜索源（如 PanSou）会偶发限流/超时，而 SDK 内部吞掉了异常只返回空列表，
    # 表现为「搜不到」。空结果时补一次，尽量把偶发失败和真的没有资源区分开。
    if not pool and queries:
        retried = True
        pool = gather()

    candidates = ai.rank_links(intent, pool[:60], client) if pool else []

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
            hint = (
                "搜索源（%s）没有返回结果：可能是被限流或超时，也可能确实没有资源。"
                "建议稍后重试，或换个更宽泛的关键词。" % "、".join(sources)
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
                "pool_size": len(pool),
                "candidates": candidates,
                "baseline": baseline,
                "resolved": resolved,
                "hint": hint,
            },
        }
    )


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


def _episode_conflict(new_name, mr, dir_se):
    """集号交叉比对：库里已有同一集（哪怕文件名不同）时返回冲突描述。

    这是「追更」场景的关键判定——仅比较完整文件名会漏掉
    「库里叫 E09.mp4、新算出来叫 剧名.S01E09.mp4」这类实际重复。
    """
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
        # 与转存引擎一致：只有一个文件夹时自动深入一层
        if len(share_list) == 1 and share_list[0].get("dir"):
            inner = account.get_detail(pwd_id, stoken, share_list[0]["fid"])
            if inner.get("code") == 0 and inner["data"].get("list"):
                share_list = inner["data"]["list"]
    except Exception as e:
        return jsonify({"success": False, "message": f"解析分享失败：{e}"})

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

    # 库里已有文件的季集索引，用于识别「同一集但文件名不同」的重复
    dir_se = []
    for _name in dir_names:
        _s, _e = _extract_se(_name, mr)
        if _e is not None:
            dir_se.append((_s, _e))

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
        }
        if is_dir:
            exists = name in dir_names
            row.update(
                {
                    "status": "folder",
                    "status_text": "文件夹（整体转存）",
                    "in_rule": True,
                    "exists": exists,
                    "default_selected": not exists,
                }
            )
        else:
            in_rule = (
                bool(re.search(rule_pattern, name))
                if (rule_pattern and rule_ok)
                else True
            )
            exists_orig = bool(mr.is_exists(name, dir_names))
            new_name = mr.sub(rule_pattern, rule_replace, name) if rule_ok else name
            exists_new = bool(mr.is_exists(new_name, dir_names))
            row["new_name"] = new_name or name
            row["in_rule"] = in_rule
            row["exists"] = exists_orig or exists_new
            if not rule_ok:
                row.update({"status": "rule_error", "status_text": "规则无效"})
            elif exists_orig:
                row.update({"status": "exists", "status_text": "已在库（同名）"})
            elif exists_new:
                row.update({"status": "exists", "status_text": "已在库（改名后同名）"})
            elif not in_rule:
                row.update({"status": "unmatched", "status_text": "不匹配规则"})
            else:
                conflict = _episode_conflict(row["new_name"], mr, dir_se)
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

    stats = {
        "total": len(rows),
        "new": sum(1 for r in rows if r["status"] == "new"),
        "exists": sum(1 for r in rows if r["status"] == "exists"),
        "dup_episode": sum(1 for r in rows if r["status"] == "dup_episode"),
        "unmatched": sum(1 for r in rows if r["status"] == "unmatched"),
        "folder": sum(1 for r in rows if r["status"] == "folder"),
    }

    data = {
        "rows": rows,
        "stats": stats,
        "savepath": savepath_norm,
        "savepath_exists": savepath_exists,
        "dir_note": dir_note,
        "dir_file_count": len(dir_names),
        "dir_files": dir_names[:100],
        "rule": {
            "pattern": pattern,
            "replace": replace,
            "resolved_pattern": rule_pattern,
            "resolved_replace": rule_replace,
            "valid": rule_ok,
        },
    }

    # ④ 可选：推荐命名规则
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

    fid_list = [f.get("fid") for f in files if f.get("fid")]
    if not fid_list:
        return jsonify({"success": False, "message": "勾选的条目缺少 fid"})

    # 最终文件名映射：值为空字符串表示保持原文件名
    rename_map = {}
    for f in files:
        orig = f.get("file_name") or ""
        if orig:
            rename_map[orig] = (f.get("new_name") or "").strip()

    savepath_norm = re.sub(r"/{2,}", "/", f"/{savepath}")
    # 目标目录不存在则创建（新剧入库场景）
    try:
        if not account.get_fids([savepath_norm]):
            account.mkdir(savepath_norm)
    except Exception:
        pass

    task = {
        "taskname": taskname,
        "shareurl": shareurl,
        "savepath": savepath_norm,
        # 成员资格由用户勾选（fid_list）决定；pattern 放开为全匹配，
        # 文件名一律由 rename_map 给出，避免规则再次筛掉用户选中的文件
        "pattern": ".*",
        "replace": "",
        "fid_list": fid_list,
        "rename_map": rename_map,
    }

    import contextlib
    import io

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            tree = account.do_save_task(task)
        ok = bool(tree)
    except Exception as e:
        buf.write(f"\n转存异常：{e}\n{traceback.format_exc()}")
        ok = False

    return jsonify(
        {
            "success": True,
            "data": {
                "ok": ok,
                "renamed": len(rename_map),
                "log": buf.getvalue().strip()[-4000:],
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
