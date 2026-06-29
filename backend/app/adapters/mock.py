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
        return {
            "upload_total": 12.8 * 1024**4,
            "download_total": 3.4 * 1024**4,
            "bonus": 98234.5,
            "ratio": 3.76,
            "active_uploads": 42,
            "active_downloads": 3,
            "bonus_per_hour_label": "最近 1 小时魔力增量（应用计算）",
            "source": "M-Team 原始数据（Mock）",
            "updated_at": datetime.utcnow().isoformat(),
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
    def search_media(self, query: str) -> list[dict[str, Any]]:
        return [
            {
                "id": f"movie-{index}",
                "media_type": "movie" if index % 2 == 0 else "tv",
                "title": f"{query or '示例标题'} {index + 1}",
                "original_title": f"原始标题 {index + 1}",
                "year": 2020 + index,
                "rating": 8.1 - index * 0.2,
                "genres": ["Drama", "Sci-Fi"],
                "poster": POSTERS[index % len(POSTERS)],
            }
            for index in range(6)
        ]

    def get_media_details(self, media_id: str, media_type: str) -> dict[str, Any]:
        return {
            "id": media_id,
            "media_type": media_type,
            "title": "示例媒体详情",
            "overview": "这里是为真实 TMDB 适配器预留的详情数据结构。",
            "cast": ["演员 A", "演员 B"],
            "similar": self.search_media("相似")[:3],
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
