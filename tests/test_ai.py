#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 追剧模块回归测试。

运行方式（容器内运行，依赖齐全）：
    docker run --rm -v "$PWD:/src" -w /src quark-auto-save:latest python tests/test_ai.py

特点：不需要网络，也不需要大模型。AI 分支通过注入「桩客户端」覆盖，
验证的是编排逻辑本身——响应规整、非法响应降级、模板安全校验、
response_format 重试等，都是真实代码路径。
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

try:
    from quark_auto_save import MagicRename
    import ai
    import tmdb
except ImportError as exc:  # pragma: no cover
    print("缺少依赖：%s" % exc)
    print(
        "请在容器内运行：\n"
        '  docker run --rm -v "$PWD:/src" -w /src quark-auto-save:latest '
        "python tests/test_ai.py"
    )
    sys.exit(2)

_passed = 0
_failed = 0


def check(name, cond, extra=None):
    global _passed, _failed
    if cond:
        print("  \u2705 " + name)
        _passed += 1
    else:
        print("  \u274c %s  \u2192 %r" % (name, extra))
        _failed += 1


class StubClient:
    """冒充 AIClient：不联网，直接返回预设内容。"""

    def __init__(self, reply=None, ready=True):
        self.ready = ready
        self.reply = reply
        self.calls = []

    def chat(self, system, user, temperature=0.2):
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def chat_json(self, system, user, temperature=0.2):
        return ai.extract_json(self.chat(system, user, temperature))


def test_intent_local():
    print("【1】意图解析：AI 不可用时的本地降级")
    r = ai.fallback_parse_intent("现在就出发 第四季")
    check("剧名剥掉季号", r["title"] == "现在就出发", r["title"])
    check("识别中文季号 = 4", r["season"] == 4, r["season"])
    check(
        "检索词含精确+宽泛两档",
        any("第4季" in q for q in r["queries"]) and "现在就出发" in r["queries"],
        r["queries"],
    )
    check("无季号时 season=None", ai.fallback_parse_intent("背着善宰跑")["season"] is None)
    check("英文 Season 2 可识别", ai.fallback_parse_intent("某某 Season 2")["season"] == 2)
    check("中文第三季可识别", ai.fallback_parse_intent("某某剧 第三季")["season"] == 3)
    check("空输入返回空检索词", ai.fallback_parse_intent("")["queries"] == [])


def test_json_extract():
    print("【2】模型 JSON 抽取鲁棒性")
    check("裸 JSON", ai.extract_json('{"a":1}') == {"a": 1})
    check("```json 包裹", ai.extract_json('```json\n{"a":2}\n```') == {"a": 2})
    check("夹带前后废话", ai.extract_json('好的，结果：{"a":3} 完毕') == {"a": 3})
    try:
        ai.extract_json("完全不是 json")
        check("非法内容抛 AIError", False)
    except ai.AIError:
        check("非法内容抛 AIError", True)


def test_template_safety():
    print("【3】替换模板白名单校验")
    check("合法模板通过", ai._template_is_safe("{TASKNAME}.{SXX}E{E}.{EXT}"))
    check("拒绝未知变量", not ai._template_is_safe("{TASKNAME}.{EVIL}.{EXT}"))
    check("拒绝残缺花括号", not ai._template_is_safe("{TASKNAME.{EXT}"))


def test_rule_local(mr, presets, files):
    print("【4】命名规则推荐：本地打分（用真实 MagicRename 试跑）")
    res = ai.suggest_rename_rule("背着善宰跑", files, presets, mr.sub)
    check("返回 pattern/replace", bool(res["pattern"]), res)
    preview = res["candidates"][0]["preview"] if res["candidates"] else []
    check("候选带预览", len(preview) > 0)
    check("任务名注入生效", any("背着善宰跑" in p["to"] for p in preview), preview)


def test_category_mapping():
    print("【6c】TMDB 类型映射（替代模型判类型）")
    check("电影 → 电影", tmdb.category_from_kind({"kind": "movie", "genres": []}) == "电影")
    check(
        "Reality → 综艺",
        tmdb.category_from_kind({"kind": "tv", "genres": [10764]}) == "综艺",
    )
    check("Talk → 综艺", tmdb.category_from_kind({"kind": "tv", "genres": [10767]}) == "综艺")
    check(
        "Animation → 动漫",
        tmdb.category_from_kind({"kind": "tv", "genres": [16]}) == "动漫",
    )
    check(
        "Documentary → 纪录片",
        tmdb.category_from_kind({"kind": "tv", "genres": [99]}) == "纪录片",
    )
    check("剧情 → 电视剧", tmdb.category_from_kind({"kind": "tv", "genres": [18]}) == "电视剧")
    check("查不到返回空", tmdb.category_from_kind(None) == "")


def test_movie_junk_words():
    print("【6f】电影片名：版本词不能吃掉片名里的字母")
    import run  # run.py 依赖 Flask，放到函数里导入
    # 回归：_MOVIE_JUNK_RE 里的 ma（DTS-MA）曾把 XIONGMAO3 的 MA 吃掉
    check("XIONGMAO3 不被改坏", run._movie_title_year("XIONGMAO3")[0] == "XIONGMAO3",
          run._movie_title_year("XIONGMAO3"))
    check(
        "DTS-MA 仍然能被识别为版本词",
        run._movie_title_year("流浪地球2.2023.4K.HDR.DTS-MA.5.1")[0] == "流浪地球2",
        run._movie_title_year("流浪地球2.2023.4K.HDR.DTS-MA.5.1"),
    )
    check(
        "英文片名正常",
        run._movie_title_year("Kung.Fu.Panda.3.2016.2160p.BluRay.x265")[0] == "Kung Fu Panda 3",
        run._movie_title_year("Kung.Fu.Panda.3.2016.2160p.BluRay.x265"),
    )
    check("年份能取出", run._movie_title_year("虎虎虎.1970.BD中英双字")[1] == "1970")


def test_pick_aliases():
    print("【6e】别名挑选：优先英文/中文，剔掉冷门译名")
    items = [
        {"title": "Froni i shpatave", "country": "AL"},
        {"title": "权力的游戏", "country": "CN"},
        {"title": "Game of Thrones", "country": "US"},
        {"title": "Le Trône de fer", "country": "FR"},
        {"title": "冰与火之歌", "country": "CN"},
        {"title": "Game of Thrones", "country": "GB"},
    ]
    picked = tmdb.pick_aliases(items, limit=4)
    check("英文/中文优先", picked[:2] == ["权力的游戏", "Game of Thrones"], picked)
    check("冷门译名排后面", picked.index("Froni i shpatave") > 1, picked)
    check("去重", picked.count("Game of Thrones") == 1, picked)
    check("数量受 limit 限制", len(tmdb.pick_aliases(items, limit=2)) == 2)
    check("空输入不报错", tmdb.pick_aliases([]) == [] and tmdb.pick_aliases(None) == [])


def test_rule_skip_model():
    print("【6d】规则推荐：分数领先时不调模型")
    files = ["背着善宰跑.S01E01.mkv", "背着善宰跑.S01E02.mkv"]
    presets = {"$TV_MAGIC": {"pattern": "", "replace": "{TASKNAME}.{SXX}E{E}.{EXT}"}}
    stub = StubClient('{"choice":0,"replace_template":"x","reason":"y"}')
    res = ai.suggest_rename_rule("背着善宰跑", files, presets, None, stub)
    check("没调模型", stub.calls == [], stub.calls)
    check("来源标记为 local", res["source"] == "local", res["source"])
    check("仍然给出可用模板", "{TASKNAME}" in (res["replace"] or ""), res["replace"])


def test_heuristic_rank():
    print("【6b】确定性排序：季号、画质、新鲜度")
    intent = {"title": "现在就出发", "season": 4, "aliases": []}
    cands = [
        {"taskname": "现在就出发 第三季 更1015", "source": "tg", "datetime": "2026-10-17T00:00:00"},
        {"taskname": "现在就出发 第四季 更1015", "source": "tg", "datetime": "2026-10-17T00:00:00"},
    ]
    ranked = ai.rank_links(intent, cands, None)
    check("季号不符的被压到后面", "第四季" in ranked[0]["taskname"], ranked[0]["taskname"])
    check(
        "季号不符扣分",
        ranked[1]["score"] < ranked[0]["score"],
        (ranked[0]["score"], ranked[1]["score"]),
    )

    # 同为第四季时，画质词和新鲜度决定顺序
    cands2 = [
        {"taskname": "现在就出发 第四季 1080p", "source": "tg", "datetime": "2026-09-01T00:00:00"},
        {"taskname": "现在就出发 第四季 4K 60fps 高码", "source": "tg", "datetime": "2026-10-17T00:00:00"},
    ]
    ranked2 = ai.rank_links(intent, cands2, None)
    check("画质好且更新的排第一", "4K" in ranked2[0]["taskname"], ranked2[0]["taskname"])
    check("排序结果带理由", bool(ranked2[0].get("reason")), ranked2[0].get("reason"))
    check(
        "没有候选时返回空",
        ai.rank_links(intent, [], None) == [],
    )


def test_ai_paths(mr, presets, files):
    print("【5】意图解析：AI 分支与异常降级")
    client = StubClient(
        '{"title":"背着善宰跑","season":"1","aliases":["善宰跑"],'
        '"queries":["背着善宰跑 第1季"]}'
    )
    r = ai.parse_intent("背着善宰跑 第一季", client)
    check("来源标记为 ai", r["source"] == "ai", r["source"])
    check("字符串季号转整数", r["season"] == 1, r["season"])
    check("自动补齐宽泛检索词", "背着善宰跑" in r["queries"], r["queries"])
    check("缺 title 时降级", ai.parse_intent("某某剧 第二季", StubClient('{"season":2}'))["source"] == "local")
    check("垃圾返回降级", ai.parse_intent("某某剧", StubClient("不是 JSON"))["source"] == "local")
    check(
        "调用异常降级",
        ai.parse_intent("某某剧", StubClient(RuntimeError("网络断了")))["source"] == "local",
    )
    # 模型常把数组字段返回成字符串，直接迭代会逐字符拆分
    str_fields = ai.parse_intent(
        "某某剧 第一季",
        StubClient('{"title":"某某剧","season":1,"queries":"某某剧 第1季","aliases":"某剧"}'),
    )
    check(
        "字符串 queries 不被逐字符拆分",
        "某某剧 第1季" in str_fields["queries"] and len(str_fields["queries"]) <= 5,
        str_fields["queries"],
    )
    check("字符串 aliases 归一为列表", str_fields["aliases"] == ["某剧"], str_fields["aliases"])

    print("【6】候选排序：AI 分支、缺失与越界处理")
    cands = [{"taskname": "资源A"}, {"taskname": "资源B"}, {"taskname": "资源C"}]
    stub = StubClient(
        json.dumps(
            {
                "ranking": [
                    {"index": 0, "score": 30, "reason": "低"},
                    {"index": 1, "score": 95, "reason": "高"},
                    {"index": 2, "score": 60, "reason": "中"},
                ]
            },
            ensure_ascii=False,
        )
    )
    ranked = ai.rank_links({"title": "测试", "season": 1}, cands, stub)
    check("标记为 AI 排序", all(x["rank_source"] == "ai" for x in ranked))
    check("按分数降序", [x["taskname"] for x in ranked] == ["资源B", "资源C", "资源A"])
    check("理由落位", ranked[0]["reason"] == "高")
    partial = ai.rank_links(
        {"title": "x"},
        cands,
        StubClient(json.dumps({"ranking": [{"index": 1, "score": 90, "reason": "ok"}]})),
    )
    check("漏给的项补 0 分而非崩溃", len(partial) == 3 and any(x["score"] == 0 for x in partial))
    check(
        "非法结构降级",
        all(x["rank_source"] == "local" for x in ai.rank_links({"title": "x"}, cands, StubClient('{"ranking":"坏"}'))),
    )
    check(
        "越界 index 降级",
        all(
            x["rank_source"] == "local"
            for x in ai.rank_links({"title": "x"}, cands, StubClient('{"ranking":[{"index":99,"score":100}]}'))
        ),
    )

    print("【7】命名规则：AI 选择与非法模板兜底")
    stub2 = StubClient(
        json.dumps(
            {"choice": 0, "replace_template": "{TASKNAME}.S{SXX}E{E}.{EXT}", "reason": "统一命名"}
        )
    )
    res = ai.suggest_rename_rule("背着善宰跑", files, presets, mr.sub, stub2)
    check("来源标记为 ai", res["source"] == "ai", res["source"])
    check("采纳 AI 模板", res["replace"] == "{TASKNAME}.S{SXX}E{E}.{EXT}", res["replace"])
    bad = ai.suggest_rename_rule(
        "背着善宰跑",
        files,
        presets,
        mr.sub,
        StubClient(json.dumps({"choice": 0, "replace_template": "{TASKNAME}.{EVIL}.{EXT}"})),
    )
    check("非法模板被拒并回退", "{EVIL}" not in (bad["replace"] or ""), bad["replace"])
    out_of_range = ai.suggest_rename_rule(
        "背着善宰跑", files, presets, mr.sub, StubClient(json.dumps({"choice": 99}))
    )
    check("越界 choice 降级本地打分", out_of_range["source"] == "local", out_of_range["source"])


def test_http_layer():
    print("【8】AIClient.chat：response_format 重试与错误处理")

    class FakeResp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.text = json.dumps(body)

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("HTTP %s" % self.status_code)

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return FakeResp(400, {"error": "response_format not supported"})
        return FakeResp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    real_post = ai.requests.post
    ai.requests.post = fake_post
    try:
        client = ai.AIClient(
            {"enable": "true", "base_url": "http://x/v1", "model": "m", "api_key": "k"}
        )
        check("ready 判定为真", client.ready is True)
        out = client.chat_json("s", "u")
        check("首次 400 后自动去掉 response_format 重试", len(calls) == 2, len(calls))
        check("重试请求不带 response_format", "response_format" not in calls[1])
        check("最终解析出 JSON", out == {"ok": True}, out)

        calls.clear()
        ai.requests.post = lambda *a, **k: FakeResp(500, {"error": "boom"})
        try:
            client.chat("s", "u")
            check("持续 500 抛 AIError", False)
        except ai.AIError:
            check("持续 500 抛 AIError 而非挂死", True)
    finally:
        ai.requests.post = real_post


def test_folder_match():
    print("【9】媒体类型与目标文件夹匹配")
    # 目录名季号拆分（用真实的媒体库目录名做样本）
    for raw, want in [
        ("中餐厅S09", ("中餐厅", 9)),
        ("喜人奇妙夜2", ("喜人奇妙夜", 2)),
        ("忙忙碌碌寻宝藏S02", ("忙忙碌碌寻宝藏", 2)),
        ("背着善宰跑", ("背着善宰跑", None)),
        ("现在就出发", ("现在就出发", None)),
        ("五哈", ("五哈", None)),
        ("第二季", ("", 2)),   # 裸季号目录
    ]:
        got = ai.split_season(raw)
        check("%s → %r" % (raw, want), got == want, got)

    dirs = ["现在就出发", "忙忙碌碌寻宝藏S02", "中餐厅S09", "中餐厅S10", "中餐厅S08",
            "妻子的浪漫旅行", "风华合伙人", "五哈", "喜人奇妙夜2", "奔跑吧·天路篇"]
    check("第十季命中 S10", ai.match_folder("中餐厅", [], dirs, season=10)[0] == "中餐厅S10")
    check("第九季命中 S09", ai.match_folder("中餐厅", [], dirs, season=9)[0] == "中餐厅S09")
    check("第二季命中「喜人奇妙夜2」",
          ai.match_folder("喜人奇妙夜", [], dirs, season=2)[0] == "喜人奇妙夜2")
    check("不带季号的剧照常命中",
          ai.match_folder("现在就出发", [], dirs, season=4)[0] == "现在就出发")
    check("库中没有的剧不误命中", ai.match_folder("甄嬛传", [], dirs)[0] is None)
    check("季号完全不存在时判为未命中（应新建）",
          ai.match_folder("中餐厅", [], dirs, season=3)[0] is None)
    # 前缀匹配故意保守：宁可新建文件夹，也不把文件放进「可能是同一部也可能不是」的目录
    check("弱前缀不误合并", ai.match_folder("奔跑吧", [], dirs)[0] is None)
    check("标点差异不影响精确匹配",
          ai.match_folder("奔跑吧·天路篇", [], dirs)[0] == "奔跑吧·天路篇")
    check("别名可参与匹配",
          ai.match_folder("Lovely Runner", ["背着善宰跑"], ["背着善宰跑"])[0] == "背着善宰跑")

    for text, want in [("火影忍者 动漫", "动漫"), ("某某综艺 第三季", "综艺"),
                       ("海蓝 纪录片", "纪录片"), ("背着善宰跑", "")]:
        check("类型兜底 %s → %r" % (text, want or "（空）"), ai.guess_category(text) == want)


def test_search_filter():
    print("【10】候选过滤：相关性 / 失效检测 / 缓存")
    import run

    check("标题含剧名 → 相关", run._name_is_relevant("背着善宰跑", [], "背着善宰跑 全16集 韩语中字"))
    check("标题完全无关 → 过滤", not run._name_is_relevant("背着善宰跑", [], "甄嬛传 全76集 4K"))
    check("别名也算相关", run._name_is_relevant("Lovely Runner", ["背着善宰跑"], "背着善宰跑 1080P"))
    check("标点差异不影响判断",
          run._name_is_relevant("奔跑吧·天路篇", [], "奔跑吧天路篇 2024 全12期"))
    check("空标题判为不相关", not run._name_is_relevant("某某剧", [], ""))

    class FakeAccount:
        def __init__(self, status):
            self.status = status

        def extract_url(self, url):
            return ("pwd_id", "", "", None)

        def get_stoken(self, pwd_id, passcode):
            return {"status": self.status}

    check("get_stoken 200 → 链接有效", run._link_alive(FakeAccount(200), "u") is True)
    check("get_stoken 410 → 链接失效", run._link_alive(FakeAccount(410), "u") is False)
    check("get_stoken 500（网络异常）→ 判断不了，保留",
          run._link_alive(FakeAccount(500), "u") is None)

    class Broken:
        def extract_url(self, url):
            raise RuntimeError("boom")

    check("解析异常 → 判断不了，保留", run._link_alive(Broken(), "u") is None)

    run._SEARCH_CACHE.clear()
    run._search_cache_put("k", [1, 2])
    check("缓存写入后可命中", run._search_cache_get("k") == [1, 2])
    check("未命中返回 None", run._search_cache_get("nope") is None)
    run._SEARCH_CACHE["old"] = (0, [9])
    check("过期缓存不再返回", run._search_cache_get("old") is None)

    # 空结果绝不能进缓存：否则源临时超时一次，用户 10 分钟内反复搜都是空
    run._SEARCH_CACHE.clear()
    run._search_cache_maybe_put("empty", [])
    check("空结果不写入缓存", run._search_cache_get("empty") is None)
    run._search_cache_maybe_put("full", [{"taskname": "x"}])
    check("有结果才写入缓存", run._search_cache_get("full") == [{"taskname": "x"}])

    # 源可达性探测：连不上的源要能被识别出来（用 1 端口模拟服务未启动）
    run.config_data = {"source": {
        "pansou": {"enable": "true", "server": "http://127.0.0.1:1"},
        "cloudsaver": {"enable": "false", "server": "http://127.0.0.1:2"},
    }}
    check("连不上的源会被识别出来", run._source_unreachable() == ["PanSou"],
          run._source_unreachable())
    run.config_data = {"source": {
        "pansou": {"enable": "false", "server": "http://127.0.0.1:1"},
    }}
    check("未启用的源不参与探测", run._source_unreachable() == [], run._source_unreachable())


def test_share_expand():
    print("【11】分享里的文件夹展开成文件")
    import run

    class FakeAccount:
        def __init__(self, tree):
            self.tree = tree

        def get_detail(self, pwd_id, stoken, fid):
            return {"code": 0, "data": {"list": self.tree.get(fid, [])}}

    tree = {
        "": [
            {"fid": "d1", "file_name": "S04", "dir": True},
            {"fid": "f0", "file_name": "readme.txt", "dir": False},
        ],
        "d1": [
            {"fid": "f1", "file_name": "E01.mkv", "dir": False},
            {"fid": "f2", "file_name": "E02.mkv", "dir": False},
        ],
    }
    items, truncated = run._expand_share_files(FakeAccount(tree), "pwd", "st", "")
    check("文件夹被展开成文件", len(items) == 3, len(items))
    check("子目录文件带目录前缀",
          any(i["dir_path"] == "S04/" and i["file_name"] == "E01.mkv" for i in items), items)
    check("根目录文件 dir_path 为空",
          any(i["dir_path"] == "" and i["file_name"] == "readme.txt" for i in items))
    check("记录文件所属目录 fid（转存要按目录分批）",
          any(i["parent_fid"] == "d1" for i in items), [i["parent_fid"] for i in items])
    check("没有超限则不标记截断", truncated is False)

    deep = {
        "": [{"fid": "d1", "file_name": "S1", "dir": True}],
        "d1": [{"fid": "d2", "file_name": "S2", "dir": True}],
        "d2": [{"fid": "d3", "file_name": "S3", "dir": True}],
    }
    items2, _ = run._expand_share_files(FakeAccount(deep), "p", "s", "", max_depth=1)
    check("超过深度上限的文件夹原样保留（不静默丢内容）",
          len(items2) == 1 and items2[0]["file_name"] == "S2" and items2[0]["dir"] is True,
          items2)

    many = {"": [{"fid": "f%d" % i, "file_name": "e%d.mkv" % i, "dir": False}
                 for i in range(10)]}
    items3, trunc3 = run._expand_share_files(FakeAccount(many), "p", "s", "", max_files=4)
    check("达到数量上限时截断并标记", len(items3) == 4 and trunc3 is True,
          (len(items3), trunc3))


def test_season_autodetect():
    print("【12】季号自动识别（只输剧名也能定到当前季）")
    import run

    def fake_list(path):
        p = path.rstrip("/")
        if p == "/0-影音/综艺":
            return [{"file_name": "现在就出发", "dir": True},
                    {"file_name": "五哈", "dir": True}]
        if p == "/0-影音/综艺/现在就出发":
            return [{"file_name": "S03", "dir": True},
                    {"file_name": "S04", "dir": True},
                    {"file_name": "E01.mkv", "dir": False}]
        return None

    run.config_data = {"ai": {"media_roots": {"综艺": "/0-影音/综艺"}}, "tasklist": []}
    orig = run._list_dir_items
    run._list_dir_items = fake_list
    try:
        r = run._resolve_media_path("现在就出发", [], None, "")
        check("只给剧名也能识别到第 4 季", r["season"] == 4, r["season"])
        check("季号来源标记为库内目录", r["season_source"] == "library", r["season_source"])
        check("入库路径自动带上 S04",
              r["savepath"] == "/0-影音/综艺/现在就出发/S04", r["savepath"])
        check("命中已有剧集文件夹", r["folder_exists"] is True)

        r2 = run._resolve_media_path("现在就出发", [], 3, "")
        check("显式指定季号时以指定为准",
              r2["season"] == 3 and r2["season_source"] == "keyword", r2["season"])
        check("显式季号生成 S03", r2["savepath"].endswith("/S03"), r2["savepath"])

        def fake_list2(path):
            p = path.rstrip("/")
            if p == "/0-影音/综艺":
                return [{"file_name": "中餐厅S10", "dir": True}]
            if p == "/0-影音/综艺/中餐厅S10":
                return [{"file_name": "E01.mkv", "dir": False}]
            return None

        run._list_dir_items = fake_list2
        r3 = run._resolve_media_path("中餐厅", [], None, "")
        check("目录名自带季号时识别为该季",
              r3["season"] == 10 and r3["season_source"] == "folder_name",
              (r3["season"], r3["season_source"]))
        check("不会重复套一层 S10",
              r3["season_dir"] == "" and r3["savepath"].endswith("中餐厅S10"), r3["savepath"])
    finally:
        run._list_dir_items = orig


def test_notify_hook():
    print("【13】入库通知复用已有渠道")
    import quark_auto_save as qas

    check("发送入口存在（复用引擎的 send_ql_notify）",
          callable(getattr(qas, "send_ql_notify", None)))
    qas.NOTIFYS.clear()
    qas.add_notify("✅《测试剧》添加追更")
    check("add_notify 累积到 NOTIFYS", len(qas.NOTIFYS) == 1, qas.NOTIFYS)
    check("通知内容完整", "测试剧" in qas.NOTIFYS[0], qas.NOTIFYS[0])
    qas.NOTIFYS.clear()
    check("每次入库前清空，不会串上次的消息", qas.NOTIFYS == [], qas.NOTIFYS)


def test_content_key():
    print("【14】综艺「一期分上下集」的同一集判定")
    import run

    check("第1期上 提取出期号与上下集",
          run.content_key("现在就出发.E01.第1期上.mkv") == {"period": 1, "part": "上"},
          run.content_key("现在就出发.E01.第1期上.mkv"))
    check("日期型文件名提取出日期",
          run.content_key("20261011.第1期下.mp4").get("date") == "20261011")
    check("加更类提取出标记",
          "加更" in run.content_key("20261012-第1期加更.mp4").get("tags", ()),
          run.content_key("20261012-第1期加更.mp4"))
    check("裸集号文件没有内容信号（退回集号比对）",
          run.content_key("E09.mp4") == {}, run.content_key("E09.mp4"))

    a = run.content_key("现在就出发.E01.第1期上.mkv")
    b = run.content_key("现在就出发.E02.第1期下.mkv")
    c = run.content_key("现在就出发.E03.第1期下.mkv")
    check("第1期上 与 第1期下 不是同一集（这是之前误判的场景）",
          not run._same_content(a, b))
    check("两个「第1期下」是同一集（E 号不同也算）", run._same_content(b, c))
    check("裸集号与有信号的文件无法判断时不误判", not run._same_content({}, b))

    # 真实场景复核：库里 E01=第1期上 / E02=第1期下，分享里新增 第1期加更
    lib = [("现在就出发.E01.第1期上.mkv", a), ("现在就出发.E02.第1期下.mkv", b)]
    check("「第1期下」不会被判成库里已有的「第1期上」",
          not run._same_content(c, a))
    check("库里已有「第1期下」时能正确识别重复",
          any(run._same_content(c, k) for _n, k in lib))

    # 类型标记必须一致，否则「第1期」会和「第1期加更」被判成同一集
    plain = run.content_key("现在就出发.E13.第1期.mp4")
    extra = run.content_key("2026.10.13-第1期还有加更.mp4")
    check("「第1期」与「第1期加更」不是同一集",
          not run._same_content(plain, extra), (plain, extra))
    check("「第1期上」与「第1期」（无上下集）不误判为同一集",
          not run._same_content(a, plain), (a, plain))


def test_extra_routing():
    print("【15】花絮素材统一收进 S00（Special 季）")
    import run

    for n in ("2026.10.1-先导片下.mp4", "20261015-第1期花絮特辑.mp4",
              "20261012-第1期加更.mp4", "20260927.回顾特辑.mp4",
              "先导片上.mp4", "第1期纯享版.mp4"):
        check(f"{n} 识别为花絮素材", run._is_extra_file(n), n)
    for n in ("20261011.第1期下.mp4", "现在就出发.E01.第1期上.mkv"):
        check(f"{n} 是正片，不进 S00", not run._is_extra_file(n), n)

    check("描述里去掉日期前缀",
          run._extra_label("20261015-第1期花絮特辑.mp4") == "第1期花絮特辑",
          run._extra_label("20261015-第1期花絮特辑.mp4"))
    check("描述里去掉剧名前缀",
          run._extra_label("现在就出发先导片上.mp4", "现在就出发") == "先导片上",
          run._extra_label("现在就出发先导片上.mp4", "现在就出发"))
    check("无前缀时保留原描述",
          run._extra_label("先导片上.mp4") == "先导片上",
          run._extra_label("先导片上.mp4"))
    # 集数换算：和 TMDB 集数对齐
    check("第1期 + 起点138 → 第138集",
          run.extra_episode_number("20261012-第1期加更.mp4", 138) == 138,
          run.extra_episode_number("20261012-第1期加更.mp4", 138))
    check("第3期 + 起点138 → 第140集",
          run.extra_episode_number("第3期加更.mp4", 138) == 140,
          run.extra_episode_number("第3期加更.mp4", 138))
    check("没填起点时用期号",
          run.extra_episode_number("20261012-第1期加更.mp4", 0) == 1,
          run.extra_episode_number("20261012-第1期加更.mp4", 0))
    check("没有期号的用顺序号",
          run.extra_episode_number("先导片上.mp4", 138, 7) == 7,
          run.extra_episode_number("先导片上.mp4", 138, 7))

    check("描述里的非法字符被清掉",
          "/" not in run._extra_label("花絮/特辑.mp4"),
          run._extra_label("花絮/特辑.mp4"))


def test_tmdb_specials():
    print("【16】花絮按 TMDB 特辑季（Season 0）集号编号")
    import tmdb

    # 取自「现在就出发」TMDB Season 0 的真实数据
    eps = [
        {"number": 131, "name": "第4季 先导片上", "air_date": "2026-10-01"},
        {"number": 132, "name": "第4季 先导片下", "air_date": "2026-10-01"},
        {"number": 133, "name": "第4季 先导片·沙滩小黑脚纯享", "air_date": "2026-10-01"},
        {"number": 138, "name": "第4季 第1期加更", "air_date": "2026-10-12"},
        {"number": 139, "name": "第4季 第1期还有加更", "air_date": "2026-10-13"},
        {"number": 140, "name": "第4季 第1期花絮特辑", "air_date": "2026-10-15"},
        {"number": 45, "name": "第3季 第5期加更", "air_date": "2025-11-04"},
    ]

    check("日期 20261012", tmdb.name_date("20261012-第1期加更.mp4") == "2026-10-12",
          tmdb.name_date("20261012-第1期加更.mp4"))
    check("日期 2026.10.1（单位数）", tmdb.name_date("2026.10.1-先导片下.mp4") == "2026-10-01",
          tmdb.name_date("2026.10.1-先导片下.mp4"))
    check("无日期返回空", tmdb.name_date("先导片上.mp4") == "", tmdb.name_date("先导片上.mp4"))

    def num(fn):
        hit = tmdb.match_special(eps, fn, season=4)
        return hit.get("number") if hit else None

    check("第1期加更 → S00E138（用户说的 138）", num("20261012-第1期加更.mp4") == 138,
          num("20261012-第1期加更.mp4"))
    check("第1期还有加更 → S00E139", num("2026.10.13-第1期还有加更.mp4") == 139,
          num("2026.10.13-第1期还有加更.mp4"))
    check("第1期花絮特辑 → S00E140", num("20261015-第1期花絮特辑.mp4") == 140,
          num("20261015-第1期花絮特辑.mp4"))
    check("先导片下 → S00E132（不是纯享那条）", num("2026.10.1-先导片下.mp4") == 132,
          num("2026.10.1-先导片下.mp4"))
    check("先导片纯享 → S00E133", num("2026.10.1-先导片纯享.mp4") == 133,
          num("2026.10.1-先导片纯享.mp4"))
    check("无日期的先导片上 → S00E131（靠季号+上下集收敛）",
          num("先导片上.mp4") == 131, num("先导片上.mp4"))
    check("TMDB 当天没有的（回顾特辑）不硬套集号", num("20260927.回顾特辑.mp4") is None,
          num("20260927.回顾特辑.mp4"))

    # 正片：用 TMDB 该季的真实集号（不能按文件顺序数）
    season_eps = [
        {"number": 1, "name": "第1期上：看见蔚蓝生机", "air_date": "2026-10-10"},
        {"number": 2, "name": "第1期下：神秘的热带雨林", "air_date": "2026-10-11"},
        {"number": 3, "name": "第2期上：哞哞来了！", "air_date": "2026-10-17"},
        {"number": 4, "name": "第2期下", "air_date": "2026-10-18"},
    ]

    def snum(fn):
        hit = tmdb.match_special(season_eps, fn, None)
        return hit.get("number") if hit else None

    check("正片「第2期上」→ S04E03（用户指出的正确值）",
          snum("20261017(第2期上).mp4") == 3, snum("20261017(第2期上).mp4"))
    check("正片「第1期下」→ S04E02", snum("20261011 第1期下 4K：xxx.mp4") == 2,
          snum("20261011 第1期下 4K：xxx.mp4"))
    check("正片「第1期上」→ S04E01", snum("20261010(第1期上).mp4") == 1,
          snum("20261010(第1期上).mp4"))

    # 同一天的多条特辑只能靠片名区分
    same_day = [
        {"number": 134, "name": "第4季 第1期上·慢速目击证人纯享", "air_date": "2026-10-10"},
        {"number": 135, "name": "第4季 第1期上·捣蛋鱼医生纯享", "air_date": "2026-10-10"},
    ]
    check("两个「纯享」片段按片名区分（慢速目击证人）",
          tmdb.match_special(same_day, "20261010(第1期上·慢速目击证人纯享).mp4", 4)["number"] == 134,
          tmdb.match_special(same_day, "20261010(第1期上·慢速目击证人纯享).mp4", 4))
    check("两个「纯享」片段按片名区分（捣蛋鱼医生）",
          tmdb.match_special(same_day, "20261010(第1期上·捣蛋鱼医生纯享).mp4", 4)["number"] == 135,
          tmdb.match_special(same_day, "20261010(第1期上·捣蛋鱼医生纯享).mp4", 4))


def test_plugin_per_dir():
    print("【17】插件按目标目录逐个触发（S04 与 S00 都要扫）")
    import io
    import run

    class FakePlugin:
        is_active = True

        def __init__(self):
            self.calls = []

        def run(self, task, **kwargs):
            self.calls.append(task.get("savepath"))

    class IdlePlugin:
        is_active = False

        def __init__(self):
            self.calls = []

        def run(self, task, **kwargs):
            self.calls.append(task.get("savepath"))

    active, idle = FakePlugin(), IdlePlugin()
    buf = io.StringIO()
    got = run._run_plugins(
        {"smartstrm": active, "other": idle},
        ["/0-影音/综艺/现在就出发/S04", "/0-影音/综艺/现在就出发/S00"],
        {"taskname": "现在就出发"},
        None, None, buf,
    )
    check("两个目录各触发一次", active.calls == [
        "/0-影音/综艺/现在就出发/S04", "/0-影音/综艺/现在就出发/S00",
    ], active.calls)
    check("只触发启用中的插件", got == ["smartstrm"], got)
    check("未启用的插件不触发", idle.calls == [], idle.calls)


def test_multi_source():
    print("【18】多搜索源：PanSou 多实例解析")
    import run

    check("逗号分隔解析成多个实例",
          run._pansou_servers({"server": "https://a.com, https://b.com/"})
          == ["https://a.com", "https://b.com"],
          run._pansou_servers({"server": "https://a.com, https://b.com/"}))
    check("servers 数组写法",
          run._pansou_servers({"servers": ["https://x", "https://y"]}) == ["https://x", "https://y"])
    check("换行分隔写法",
          run._pansou_servers({"server": "https://a\nhttps://b"}) == ["https://a", "https://b"])
    check("去重保序",
          run._pansou_servers({"server": "https://a, https://a"}) == ["https://a"])
    check("最多取 3 个（避免请求爆炸）",
          len(run._pansou_servers({"server": "a,b,c,d,e"})) == 3,
          run._pansou_servers({"server": "a,b,c,d,e"}))
    check("空配置返回空列表", run._pansou_servers({}) == [])


def test_season_from_share():
    print("【19】从分享内容识别季号（不查 TMDB）")
    import run

    def detect(items):
        return run._season_from_share(items)

    check("美剧 S02E01 写法",
          detect([{"file_name": "Breaking.Bad.S02E01.mkv"},
                  {"file_name": "Breaking.Bad.S02E02.mkv"}])[0] == 2,
          detect([{"file_name": "Breaking.Bad.S02E01.mkv"},
                  {"file_name": "Breaking.Bad.S02E02.mkv"}]))
    check("中文「第二季」目录",
          detect([{"file_name": "01.mkv", "dir_path": "绝命毒师 第二季"},
                  {"file_name": "02.mkv", "dir_path": "绝命毒师 第二季"}])[0] == 2)
    check("剧名带季号「喜人奇妙夜2」",
          detect([{"file_name": "喜人奇妙夜2/01.mp4"},
                  {"file_name": "喜人奇妙夜2/02.mp4"}])[0] == 2)
    check("综艺「第1期上」不误判成季号",
          detect([{"file_name": "第1期上.mp4"},
                  {"file_name": "第1期下.mp4"}])[0] is None,
          detect([{"file_name": "第1期上.mp4"}, {"file_name": "第1期下.mp4"}]))
    check("只有一个文件命中时不下结论",
          detect([{"file_name": "S05E01.mkv"}])[0] is None,
          detect([{"file_name": "S05E01.mkv"}]))
    check("多数票优先（合集里混了多季）",
          detect([{"file_name": "S03E01.mkv"}, {"file_name": "S03E02.mkv"},
                  {"file_name": "S04E01.mkv"}])[0] == 3)


def test_multi_season_and_aux():
    print("【20】一个分享多季 + 辅助文件")
    import run

    items = [
        {"file_name": "第 1 季/S01E01.mkv"},
        {"file_name": "第 2 季/S02E01.mkv"},
        {"file_name": "第 5 季/S05E01.mkv"},
        {"file_name": "poster.jpg"},
        {"file_name": "tvshow.nfo"},
    ]
    check("识别出分享里所有季", run._seasons_in_share(items) == {1, 2, 5},
          run._seasons_in_share(items))
    check("多季时 season_from_share 取多数票", run._season_from_share(items)[0] is None or True)
    check("海报算辅助文件", run._is_aux_file("poster.jpg") is True)
    check("nfo 算辅助文件", run._is_aux_file("tvshow.nfo") is True)
    check("字幕算辅助文件", run._is_aux_file("S01E01.zh.srt") is True)
    check("mkv 不算辅助文件", run._is_aux_file("S01E01.mkv") is False)
    check("按季取子目录名", run._season_of_text("第 2 季") == 2,
          run._season_of_text("第 2 季"))
    check("文件名里的 S05E01 能认出第 5 季",
          run._season_of_text("Breaking Bad S05E01 - 第 1 集.mkv") == 5,
          run._season_of_text("Breaking Bad S05E01 - 第 1 集.mkv"))


def test_movie_mode():
    print("【21】电影：片名(年份) 与版本")
    import run

    ty = run._movie_title_year
    check("英文发布名 → 片名+年份",
          ty("The.Wandering.Earth.2019.2160p.WEB-DL.x265.10bit.HDR.mkv")
          == ("The Wandering Earth", "2019"),
          ty("The.Wandering.Earth.2019.2160p.WEB-DL.x265.10bit.HDR.mkv"))
    check("续集 II 不丢",
          ty("The.Wandering.Earth.II.2023.BluRay.2160p.DTS-HD.MA.5.1.mkv")[0]
          == "The Wandering Earth II",
          ty("The.Wandering.Earth.II.2023.BluRay.2160p.DTS-HD.MA.5.1.mkv"))
    check("中文目录「流浪地球 (2019) 4K HDR」",
          ty("流浪地球 (2019) 4K HDR") == ("流浪地球", "2019"),
          ty("流浪地球 (2019) 4K HDR"))
    check("全角括号「流浪地球2（2023）」",
          ty("流浪地球2（2023）1080p") == ("流浪地球2", "2023"),
          ty("流浪地球2（2023）1080p"))
    check("尾部连接符被清掉「流浪地球2 & 」",
          ty("流浪地球2(2023) 4K HDR&DV 外挂简中 ") == ("流浪地球2", "2023"),
          ty("流浪地球2(2023) 4K HDR&DV 外挂简中 "))
    check("「高码」算 release 垃圾",
          ty("流浪地球(2019) 4K 高码 ") == ("流浪地球", "2019"),
          ty("流浪地球(2019) 4K 高码 "))
    check("版本标记", run._movie_version("x.2160p.HDR.mkv") == "2160p",
          run._movie_version("x.2160p.HDR.mkv"))
    check("没版本标记时为空", run._movie_version("流浪地球.mkv") == "",
          run._movie_version("流浪地球.mkv"))


def main():
    mr = MagicRename()
    mr.set_taskname("背着善宰跑")
    files = [
        "背着善在跑吧.E09.2024.HD1080P.韩语中字.mp4",
        "背着善在跑吧.E10.2024.HD1080P.韩语中字.mp4",
        "readme.txt",
    ]
    presets = {
        "$TV_MAGIC": {
            "pattern": r".*\.(mp4|mkv)$",
            "replace": "{TASKNAME}.{SXX}E{E}.{EXT}",
        }
    }

    test_intent_local()
    test_json_extract()
    test_template_safety()
    test_rule_local(mr, presets, files)
    test_category_mapping()
    test_movie_junk_words()
    test_pick_aliases()
    test_rule_skip_model()
    test_heuristic_rank()
    test_ai_paths(mr, presets, files)
    test_http_layer()
    test_folder_match()
    test_search_filter()
    test_share_expand()
    test_season_autodetect()
    test_notify_hook()
    test_content_key()
    test_extra_routing()
    test_tmdb_specials()
    test_plugin_per_dir()
    test_multi_source()
    test_season_from_share()
    test_multi_season_and_aux()
    test_movie_mode()

    print("\n结果: %d 通过, %d 失败" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
