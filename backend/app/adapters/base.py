from abc import ABC, abstractmethod
from typing import Any


class TrackerAdapter(ABC):
    @abstractmethod
    def get_user_stats(self) -> dict[str, Any]: ...

    @abstractmethod
    def search_torrents(self, query: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_download_payload(self, torrent_id: str) -> dict[str, Any]: ...


class QbittorrentAdapter(ABC):
    @abstractmethod
    def get_server_state(self, downloader_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_torrents(self, downloader_id: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def add_torrent(self, downloader_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def mutate_torrent(self, downloader_id: str, torrent_hash: str, action: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class MetadataAdapter(ABC):
    @abstractmethod
    def search_media(self, query: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_media_details(self, media_id: str, media_type: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_person_details(self, person_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_discover_lists(self) -> dict[str, Any]: ...

    @abstractmethod
    def discover_media(self, filters: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def get_discover_filter_options(self) -> dict[str, Any]: ...


class AIAdapter(ABC):
    @abstractmethod
    def parse_search_intent(self, text: str) -> dict[str, Any]: ...

    @abstractmethod
    def explain_stats(self, context: dict[str, Any]) -> dict[str, Any]: ...


class NotificationAdapter(ABC):
    @abstractmethod
    def send_in_app(self, notification: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def send_wechat(self, notification: dict[str, Any]) -> dict[str, Any]: ...
