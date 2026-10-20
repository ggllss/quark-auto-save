import requests


class Emby:

    default_config = {
        "url": "",  # Emby服务器地址
        "token": "",  # Emby服务器token
    }
    default_task_config = {
        "try_match": True,  # 是否尝试匹配
        "media_id": "",  # 媒体ID，当为0时不刷新
    }
    is_active = False

    def __init__(self, **kwargs):
        self.plugin_name = self.__class__.__name__.lower()
        if kwargs:
            for key, _ in self.default_config.items():
                if key in kwargs:
                    setattr(self, key, kwargs[key])
                else:
                    print(f"{self.plugin_name} 模块缺少必要参数: {key}")
            if self.url and self.token:
                if self.get_info():
                    self.is_active = True

    def run(self, task, **kwargs):
        # 必须拷贝：default_task_config 是类级共享字典，
        # 直接改它会把上一个任务匹配到的 media_id 带到下一个任务上
        # （实测：刷完「现在就出发」再转存「功夫熊猫3」，刷的还是 1727）
        task_config = dict(
            task.get("addition", {}).get(self.plugin_name) or self.default_task_config
        )
        if media_id := task_config.get("media_id"):
            if media_id != "0":
                self.refresh(media_id)
        elif task_config.get("try_match"):
            if match_media_id := self.search(task["taskname"]):
                self.refresh(match_media_id)
                task_config["media_id"] = match_media_id
                task.setdefault("addition", {})[self.plugin_name] = task_config
                return task

    def get_info(self):
        url = f"{self.url}/emby/System/Info"
        headers = {"X-Emby-Token": self.token}
        querystring = {}
        try:
            response = requests.request("GET", url, headers=headers, params=querystring)
            if "application/json" in response.headers["Content-Type"]:
                response = response.json()
                print(
                    f"Emby媒体库: {response.get('ServerName','')} v{response.get('Version','')}"
                )
                return True
            else:
                print(f"Emby媒体库: 连接失败❌ {response.text}")
        except Exception as e:
            print(f"获取Emby媒体库信息出错: {e}")
        return False

    def refresh(self, emby_id):
        if not emby_id:
            return False
        # 同一个媒体条目一次入库只刷一次：_run_plugins 会按目录
        # （S04、S00）逐个触发插件，否则同一部剧会被刷两遍
        done = getattr(self, "_refreshed_ids", None)
        if done is None:
            done = set()
            self._refreshed_ids = done
        if emby_id in done:
            return True
        done.add(emby_id)
        url = f"{self.url}/emby/Items/{emby_id}/Refresh"
        headers = {"X-Emby-Token": self.token}
        querystring = {
            "Recursive": "true",
            # Default 会重新扫描该条目的目录并把新文件纳入库，
            # 但不会把已有元数据全部重刮（FullRefresh 对整季剧集太慢）
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
            "ReplaceAllImages": "false",
        }
        try:
            response = requests.request(
                "POST", url, headers=headers, params=querystring
            )
            if response.text == "":
                print(f"🎞️ 刷新Emby媒体库：成功✅")
                return True
            else:
                print(f"🎞️ 刷新Emby媒体库：{response.text}❌")
        except Exception as e:
            print(f"刷新Emby媒体库出错: {e}")
        return False

    def search(self, media_name):
        if not media_name:
            return ""
        url = f"{self.url}/emby/Items"
        headers = {"X-Emby-Token": self.token}
        querystring = {
            # 只搜 Series 的话电影永远匹配不到（功夫熊猫3 这种 Movie 条目），
            # 综艺在 Emby 里也是 Series，一并覆盖
            "IncludeItemTypes": "Series,Movie",
            "StartIndex": 0,
            "SortBy": "SortName",
            "SortOrder": "Ascending",
            "ImageTypeLimit": 0,
            "Recursive": "true",
            "SearchTerm": media_name,
            "Limit": 10,
            "IncludeSearchTypes": "false",
        }
        try:
            response = requests.request("GET", url, headers=headers, params=querystring)
            if "application/json" in response.headers["Content-Type"]:
                response = response.json()
                # 别用 IsFolder 过滤：电影条目的 IsFolder 是 False，
                # 会被整批挡掉（功夫熊猫3 就是这种情况）。按类型判断。
                items = [
                    i for i in (response.get("Items") or [])
                    if i.get("Type") in ("Series", "Movie", "BoxSet")
                ]
                if items:
                    # 精确同名优先，否则取第一条（搜索接口按相关度排序）
                    target = media_name.strip().lower()
                    exact = [i for i in items if (i.get("Name") or "").strip().lower() == target]
                    item = (exact or items)[0]
                    print(
                        f"🎞️ 《{item['Name']}》匹配到Emby媒体库ID：{item['Id']}"
                        f"（{item.get('Type')}）"
                    )
                    return item["Id"]
            else:
                print(f"🎞️ 搜索Emby媒体库：{response.text}❌")
        except Exception as e:
            print(f"搜索Emby媒体库出错: {e}")
        return ""
