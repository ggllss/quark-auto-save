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
    test_ai_paths(mr, presets, files)
    test_http_layer()
    test_folder_match()
    test_search_filter()
    test_share_expand()
    test_season_autodetect()
    test_notify_hook()

    print("\n结果: %d 通过, %d 失败" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
