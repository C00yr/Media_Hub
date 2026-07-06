from datetime import datetime
from typing import Any

from app.adapters.base import AIAdapter, MetadataAdapter, NotificationAdapter, QbittorrentAdapter, TrackerAdapter
from app.utils.ids import trace_id


POSTERS = [
    "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?auto=format&fit=crop&w=500&q=80",
    "https://images.unsplash.com/photo-1524985069026-dd778a71c7b4?auto=format&fit=crop&w=500&q=80",
    "https://images.unsplash.com/photo-1536440136628-849c177e76a1?auto=format&fit=crop&w=500&q=80",
]


class MockTrackerAdapter(TrackerAdapter):
    def get_user_stats(self) -> dict[str, Any]:
        upload_total = 9.03 * 1024**4
        download_total = 749.99 * 1024**3
        return {
            "user_level": "User",
            "upload_total": upload_total,
            "upload_delta_label": "+122.38 GB",
            "download_total": download_total,
            "download_delta_label": "+22.67 GB",
            "bonus": 11167.0,
            "bonus_delta_label": "+816.6",
            "ratio": 12.329,
            "ratio_delta_label": "-0.216",
            "seed_count": 14,
            "seed_count_delta_label": "+0",
            "seed_size": 313.77 * 1024**3,
            "seed_size_delta_label": "-35.23 GB",
            "joined_at": "2026-06-12",
            "active_uploads": 14,
            "active_downloads": 0,
            "bonus_per_hour_label": "最近 1 小时魔力增量（应用计算）",
            "source": "M-Team 原始数据（Mock）",
            "updated_at": datetime.utcnow().isoformat(),
            "traffic_history": [
                {
                    "date": f"2026-06-{day:02d}",
                    "upload_total": (8.74 + max(day - 26, 0) * 0.08 + index * 0.01) * 1024**4,
                    "download_total": (0.48 + max(day - 28, 0) * 0.08 + index * 0.01) * 1024**4,
                }
                for index, day in enumerate(range(15, 31))
            ],
        }

    def search_torrents(self, query: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        base = query or "Sample Movie"
        return [
            {
                "id": f"mt-{index}",
                "title": f"{base} {quality} M-Team 示例资源",
                "resolution": quality,
                "codec": "H.265",
                "hdr": "HDR10" if index == 1 else "SDR",
                "size": f"{18 + index * 8}.4 GB",
                "group": "PTMH",
                "seeders": 35 - index * 6,
                "downloads": 120 + index * 14,
                "published_at": "2026-06-29T08:00:00Z",
            }
            for index, quality in enumerate(["2160p", "1080p", "720p"])
        ]

    def get_download_payload(self, torrent_id: str) -> dict[str, Any]:
        return {"torrent_id": torrent_id, "download_url": f"mock://mteam/{torrent_id}"}


class MockQbittorrentAdapter(QbittorrentAdapter):
    def get_server_state(self, downloader_id: str) -> dict[str, Any]:
        index = int(downloader_id.replace("qb", ""))
        return {
            "id": downloader_id,
            "name": f"qB {index}",
            "online": True,
            "download_speed": 2_400_000 + index * 340_000,
            "upload_speed": 1_100_000 + index * 220_000,
            "active_downloads": 2 + index,
            "active_uploads": 11 + index * 3,
            "paused": index,
            "errors": 0,
            "free_space": 3.8 * 1024**4 - index * 100 * 1024**3,
            "total_space": 8 * 1024**4,
            "source": "qB Web API 原始数据（Mock）",
            "updated_at": datetime.utcnow().isoformat(),
        }

    def get_torrents(self, downloader_id: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "hash": f"{downloader_id}-hash-{index}",
                "name": f"{downloader_id.upper()} 示例任务 {index + 1}",
                "size": (20 + index * 4) * 1024**3,
                "progress": min(0.25 + index * 0.23, 1),
                "download_speed": 900_000 + index * 150_000,
                "upload_speed": 350_000 + index * 95_000,
                "uploaded": (40 + index * 8) * 1024**3,
                "downloaded": (12 + index * 4) * 1024**3,
                "ratio": 2.1 + index * 0.4,
                "category": "media",
                "tags": ["mock", "manual-ok"],
                "save_path": "/downloads/media/[redacted]",
                "added_at": "2026-06-29T07:30:00Z",
                "completed_at": None,
                "state": "downloading" if index < 2 else "uploading",
            }
            for index in range(3)
        ]

    def add_torrent(self, downloader_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"accepted": True, "trace_id": trace_id("DL"), "downloader_id": downloader_id, "task_hash": f"{downloader_id}-new-mock"}

    def mutate_torrent(self, downloader_id: str, torrent_hash: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"accepted": True, "trace_id": trace_id("QBACT"), "downloader_id": downloader_id, "hash": torrent_hash, "action": action}


class MockMetadataAdapter(MetadataAdapter):
    def _mock_cast(self) -> list[dict[str, Any]]:
        return [
            {
                "id": index + 1,
                "person_id": index + 1,
                "name": name,
                "character": role,
                "profile": POSTERS[index % len(POSTERS)],
                "order": index,
            }
            for index, (name, role) in enumerate([
                ("演员 A", "主角"),
                ("演员 B", "搭档"),
                ("演员 C", "特别出演"),
                ("演员 D", "配角"),
            ])
        ]

    def search_media(self, query: str) -> list[dict[str, Any]]:
        return [
            {
                "id": f"movie-{index}",
                "tmdb_id": index + 1,
                "media_type": "movie" if index % 2 == 0 else "tv",
                "title": f"{query or '示例标题'} {index + 1}",
                "original_title": f"原始标题 {index + 1}",
                "year": 2020 + index,
                "release_date": f"{2020 + index}-06-01",
                "runtime": 100 + index * 4,
                "rating": 8.1 - index * 0.2,
                "genres": ["Drama", "Sci-Fi"],
                "poster": POSTERS[index % len(POSTERS)],
                "backdrop": POSTERS[(index + 1) % len(POSTERS)],
                "production_countries": ["中国大陆"] if index % 2 == 0 else ["美国"],
                "director": f"导演 {index + 1}",
                "cast": [f"主演 {index + 1}", f"演员 {index + 2}"],
            }
            for index in range(6)
        ]

    def get_media_details(self, media_id: str, media_type: str) -> dict[str, Any]:
        return {
            "id": f"{media_type}-{media_id}",
            "tmdb_id": media_id,
            "media_type": media_type,
            "title": "示例媒体详情",
            "original_title": "Sample Detail",
            "year": "2026",
            "release_date": "2026-06-01",
            "runtime": 112,
            "rating": 8.2,
            "vote_count": 1024,
            "popularity": 92.5,
            "poster": POSTERS[0],
            "backdrop": POSTERS[1],
            "overview": "这里是为真实 TMDB 适配器预留的详情数据结构。",
            "genres": ["剧情", "科幻"],
            "director": "示例导演",
            "cast": ["演员 A", "演员 B"],
            "cast_members": self._mock_cast(),
            "production_countries": ["中国大陆"],
            "original_language": "zh",
            "recommendations": self.search_media("猜你喜欢")[:6],
        }

    def get_person_details(self, person_id: str) -> dict[str, Any]:
        return {
            "id": person_id,
            "person_id": person_id,
            "name": f"示例演员 {person_id}",
            "profile": POSTERS[int(person_id) % len(POSTERS)] if str(person_id).isdigit() else POSTERS[0],
            "biography": "这里展示演员简介，以及由 TMDB 返回的相关作品。",
            "birthday": "1988-01-01",
            "deathday": "",
            "place_of_birth": "中国",
            "known_for_department": "Acting",
            "also_known_as": [],
            "gender": None,
            "imdb_id": "",
            "known_for": self.search_media("参演作品")[:8],
        }

    def get_discover_lists(self) -> dict[str, Any]:
        return {
            "trending": self.search_media("流行趋势"),
            "popular_movies": self.search_media("热门电影"),
            "popular_tv": self.search_media("热门剧集"),
            "top_rated_movies": self.search_media("高分电影"),
            "top_rated_tv": self.search_media("高分剧集"),
            "genres": ["动作", "剧情", "科幻", "纪录片"],
        }

    def discover_media(self, filters: dict[str, Any]) -> dict[str, Any]:
        media_type = str(filters.get("media_type") or "movie")
        page = int(filters.get("page") or 1)
        pages = int(filters.get("pages") or 1)
        base_items = self.search_media("条件筛选")
        items = [
            {
                **item,
                "id": f"{item['id']}-p{page + offset}",
                "tmdb_id": int(item["tmdb_id"]) + (page + offset) * 100,
                "media_type": media_type,
                "genres": [genre["name"] for genre in self.get_discover_filter_options()["genres"].get(media_type, [])[:2]],
            }
            for offset in range(pages)
            for item in base_items
        ]
        total_pages = 6
        return {
            "source": "mock",
            "configured": False,
            "message": "请先在设置中启用 TMDB，当前展示示例筛选结果。",
            "filters": filters,
            "items": items,
            "page": page,
            "start_page": page,
            "pages": pages,
            "next_page": page + pages if page + pages <= total_pages else None,
            "total_pages": total_pages,
            "total_results": len(base_items) * total_pages,
            "options": self.get_discover_filter_options(),
        }

    def get_discover_filter_options(self) -> dict[str, Any]:
        genres = [
            {"id": "28", "name": "动作"},
            {"id": "18", "name": "剧情"},
            {"id": "878", "name": "科幻"},
            {"id": "16", "name": "动画"},
            {"id": "9648", "name": "悬疑"},
            {"id": "99", "name": "纪录片"},
        ]
        return {
            "genres": {"movie": genres, "tv": genres},
            "sorts": [
                {"value": "popularity.desc", "label": "综合排序"},
                {"value": "release_date.desc", "label": "首播时间"},
                {"value": "vote_average.desc", "label": "高分优先"},
                {"value": "vote_count.desc", "label": "讨论热度"},
            ],
            "regions": [
                {"value": "", "label": "不限地区"},
                {"value": "CN", "label": "中国大陆"},
                {"value": "US", "label": "美国"},
                {"value": "JP", "label": "日本"},
                {"value": "KR", "label": "韩国"},
            ],
            "languages": [
                {"value": "", "label": "不限语言"},
                {"value": "zh", "label": "中文"},
                {"value": "en", "label": "英语"},
                {"value": "ja", "label": "日语"},
                {"value": "ko", "label": "韩语"},
            ],
        }


class MockAIAdapter(AIAdapter):
    def parse_search_intent(self, text: str) -> dict[str, Any]:
        return {"query": text, "filters": {"resolution": "1080p+", "codec": "H.265"}, "allowed_actions": ["search_only"]}

    def explain_stats(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"summary": "Mock 说明。AI 不会调用破坏性操作。", "context_keys": list(context.keys())}


class MockNotificationAdapter(NotificationAdapter):
    def send_in_app(self, notification: dict[str, Any]) -> dict[str, Any]:
        return {"sent": True, "channel": "in_app", "trace_id": trace_id("NTF")}

    def send_wechat(self, notification: dict[str, Any]) -> dict[str, Any]:
        return {"sent": True, "channel": "wechat_claw_mock", "trace_id": trace_id("NTF")}
