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

    print("\n结果: %d 通过, %d 失败" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
