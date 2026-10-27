"""表情包 / 图片采集插件的数据模型与存储层。

设计要点：
- 所有数据存放在 AstrBot 的 ``data/plugin_data/<plugin_name>/`` 目录下，
  插件卸载重装不会丢数据。
- 使用单个 SQLite 文件做索引，图片本体以文件形式落盘，便于图库界面直接引用。
- 采用 ``asyncio.Lock`` 串行化所有写操作，避免多事件并发写库导致锁冲突。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # 作为插件包加载
    from .imageinfo import _jpeg_size, _webp_size
except ImportError:  # 独立运行单元测试时
    from imageinfo import _jpeg_size, _webp_size

# 允许的图片后缀。QQ 表情包一般是 gif/png/jpg/webp。
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# 单文件大小上限（10MB），防止把视频/大图当图片存爆磁盘。
MAX_FILE_BYTES = 10 * 1024 * 1024
# 素材总容量上限（2GB）。
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def _now() -> float:
    return time.time()


@dataclass
class Asset:
    """一条采集到的图片素材记录。"""

    id: int
    sha256: str
    filename: str
    stored_name: str
    mime: str
    size: int
    width: int
    height: int
    source: str
    """来源类型：``image``（图片消息）/ ``face``（QQ 系统表情）/ ``sticker``（NapCat 动画表情）。"""

    sender_id: str
    sender_name: str
    group_id: str
    session_id: str
    created_at: float
    duration_ms: int = 0
    """动画表情时长（毫秒），0 表示静态图。"""

    @property
    def is_animated(self) -> bool:
        return self.duration_ms > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sha256": self.sha256,
            "filename": self.filename,
            "mime": self.mime,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "group_id": self.group_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "duration_ms": self.duration_ms,
            "is_animated": self.is_animated,
        }


@dataclass
class Query:
    """图库查询条件。"""

    keyword: str = ""
    sender_id: str = ""
    source: str = ""
    group_id: str = ""
    animated_only: bool = False
    static_only: bool = False
    order: str = "new"  # new | old | big | small
    page: int = 1
    page_size: int = 48


@dataclass
class Page:
    """分页查询结果。"""

    items: list[Asset] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 48
    total_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "total_bytes": self.total_bytes,
        }


class Storage:
    """素材存储：SQLite 索引 + 文件本体。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "assets.db"
        self.media_dir = self.root / "media"
        self.lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ensure_dirs()
        self._init_db()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def _init_db(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'image',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_sender ON assets(sender_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_sha ON assets(sha256)"
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def _guess_mime(self, filename: str, data: bytes) -> str:
        """判定图片类型。

        **优先按内容字节判定**，因为 QQ 表情包的地址后缀并不可靠
        （``p.qpic.cn`` 的 ``/0`` 结尾实际返回的是 JPEG）。
        只有在无法从内容判定时，才退回文件名的扩展名。
        """
        sniffed = self._sniff_mime(data)
        if sniffed:
            return sniffed
        mime, _ = mimetypes.guess_type(filename)
        if mime and mime.startswith("image/"):
            return mime
        return "application/octet-stream"

    @staticmethod
    def _sniff_mime(data: bytes) -> str:
        """用魔数判断图片类型，认不出返回空字符串。"""
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:2] == b"BM":
            return "image/bmp"
        return ""

    @staticmethod
    def _suffix_of(filename: str, mime: str) -> str:
        """按 MIME 决定落盘后缀，保证扩展名和真实内容一致。"""
        by_mime = {
            "image/gif": ".gif",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }
        if mime in by_mime:
            return by_mime[mime]
        suffix = Path(filename).suffix.lower()
        if suffix in ALLOWED_SUFFIXES:
            return suffix
        return ".bin"

    @staticmethod
    def _read_image_size(data: bytes) -> tuple[int, int]:
        """不依赖 Pillow 读取图片尺寸，失败时返回 (0, 0)。

        支持 PNG / GIF / JPEG / BMP / WEBP(VP8X/VP8/VP8L)。
        """
        try:
            if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
                width = int.from_bytes(data[16:20], "big")
                height = int.from_bytes(data[20:24], "big")
                return width, height
            if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
                width = int.from_bytes(data[6:8], "little")
                height = int.from_bytes(data[8:10], "little")
                return width, height
            if data[:2] == b"BM" and len(data) >= 26:
                width = int.from_bytes(data[18:22], "little", signed=True)
                height = int.from_bytes(data[22:26], "little", signed=True)
                return abs(width), abs(height)
            if data[:2] == b"\xff\xd8":
                return _jpeg_size(data)
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                return _webp_size(data)
        except Exception:  # noqa: BLE001 - 尺寸只是附加信息，失败不应影响采集
            pass
        return 0, 0

    def _total_bytes(self) -> int:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS total FROM assets"
        ).fetchone()
        return int(row["total"] or 0)

    async def add_bytes(
        self,
        data: bytes,
        *,
        filename: str = "image.bin",
        source: str = "image",
        sender_id: str = "",
        sender_name: str = "",
        group_id: str = "",
        session_id: str = "",
        duration_ms: int = 0,
    ) -> Asset | None:
        """写入一份素材。内容重复（sha256 相同）时返回已有的记录，不重复占用空间。"""
        if not data:
            return None
        if len(data) > MAX_FILE_BYTES:
            return None

        digest = hashlib.sha256(data).hexdigest()
        mime = self._guess_mime(filename, data)
        if not mime.startswith("image/"):
            return None
        suffix = self._suffix_of(filename, mime)
        width, height = self._read_image_size(data)

        async with self.lock:
            assert self._conn is not None
            existing = self._conn.execute(
                "SELECT * FROM assets WHERE sha256 = ?", (digest,)
            ).fetchone()
            if existing is not None:
                return self._row_to_asset(existing)

            if self._total_bytes() + len(data) > MAX_TOTAL_BYTES:
                return None

            stored_name = f"{digest[:2]}/{digest}{suffix}"
            stored_path = self.media_dir / stored_name
            stored_path.parent.mkdir(parents=True, exist_ok=True)
            stored_path.write_bytes(data)

            created = _now()
            cursor = self._conn.execute(
                """
                INSERT INTO assets (
                    sha256, filename, stored_name, mime, size, width, height,
                    source, sender_id, sender_name, group_id, session_id,
                    created_at, duration_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    digest,
                    filename,
                    stored_name,
                    mime,
                    len(data),
                    width,
                    height,
                    source,
                    sender_id,
                    sender_name,
                    group_id,
                    session_id,
                    created,
                    duration_ms,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM assets WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_asset(row)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> Asset:
        return Asset(
            id=int(row["id"]),
            sha256=str(row["sha256"]),
            filename=str(row["filename"]),
            stored_name=str(row["stored_name"]),
            mime=str(row["mime"]),
            size=int(row["size"]),
            width=int(row["width"]),
            height=int(row["height"]),
            source=str(row["source"]),
            sender_id=str(row["sender_id"]),
            sender_name=str(row["sender_name"]),
            group_id=str(row["group_id"]),
            session_id=str(row["session_id"]),
            created_at=float(row["created_at"]),
            duration_ms=int(row["duration_ms"]),
        )

    def _build_where(self, query: Query) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.keyword:
            clauses.append(
                "(filename LIKE ? OR sender_name LIKE ? OR sha256 LIKE ? OR sender_id LIKE ?)"
            )
            like = f"%{query.keyword}%"
            params.extend([like, like, like, like])
        if query.sender_id:
            clauses.append("sender_id = ?")
            params.append(query.sender_id)
        if query.source:
            clauses.append("source = ?")
            params.append(query.source)
        if query.group_id:
            clauses.append("group_id = ?")
            params.append(query.group_id)
        if query.animated_only:
            clauses.append("duration_ms > 0")
        if query.static_only:
            clauses.append("duration_ms = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_assets(self, query: Query) -> Page:
        where, params = self._build_where(query)
        order_map = {
            "new": "created_at DESC, id DESC",
            "old": "created_at ASC, id ASC",
            "big": "size DESC, id DESC",
            "small": "size ASC, id ASC",
        }
        order = order_map.get(query.order, order_map["new"])
        page = max(1, int(query.page))
        page_size = max(1, min(200, int(query.page_size)))
        offset = (page - 1) * page_size

        assert self._conn is not None
        total_row = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM assets{where}", params
        ).fetchone()
        total = int(total_row["c"] or 0)
        rows = self._conn.execute(
            f"SELECT * FROM assets{where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return Page(
            items=[self._row_to_asset(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            total_bytes=self._total_bytes(),
        )

    def get_asset(self, asset_id: int) -> Asset | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_asset(row)

    def find_by_sha(self, digest: str) -> Asset | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM assets WHERE sha256 = ?", (digest,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_asset(row)

    def media_path(self, stored_name: str) -> Path:
        """根据库中记录的相对路径定位文件，防止路径穿越。

        只接受 ``<2位十六进制>/<sha256>.<后缀>`` 这种由本插件生成的格式，
        任何包含 ``..``、绝对路径或多余层级的一律拒绝。
        """
        raw = str(stored_name).replace("\\", "/").strip()
        parts = [part for part in raw.split("/") if part not in ("", ".", "..")]
        if len(parts) != 2:
            raise ValueError("非法路径")
        folder, filename = parts
        if len(folder) != 2 or not all(char in "0123456789abcdef" for char in folder):
            raise ValueError("非法路径")
        if filename != Path(filename).name or ".." in filename:
            raise ValueError("非法路径")
        if "/" in filename or "\\" in filename:
            raise ValueError("非法路径")

        target = (self.media_dir / folder / filename).resolve(strict=False)
        try:
            target.relative_to(self.media_dir.resolve(strict=False))
        except ValueError:
            raise ValueError("非法路径") from None
        return target

    def list_senders(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT sender_id, sender_name, COUNT(*) AS c, SUM(size) AS s
            FROM assets
            GROUP BY sender_id
            ORDER BY c DESC
            """
        ).fetchall()
        return [
            {
                "sender_id": str(row["sender_id"]),
                "sender_name": str(row["sender_name"] or ""),
                "count": int(row["c"]),
                "size": int(row["s"] or 0),
            }
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS c,
                   COALESCE(SUM(size), 0) AS s,
                   COALESCE(SUM(CASE WHEN duration_ms > 0 THEN 1 ELSE 0 END), 0) AS a
            FROM assets
            """
        ).fetchone()
        source_rows = self._conn.execute(
            "SELECT source, COUNT(*) AS c FROM assets GROUP BY source"
        ).fetchall()
        return {
            "count": int(row["c"] or 0),
            "total_bytes": int(row["s"] or 0),
            "animated_count": int(row["a"] or 0),
            "by_source": {str(r["source"]): int(r["c"]) for r in source_rows},
        }

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    async def delete_asset(self, asset_id: int) -> bool:
        async with self.lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT stored_name FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            self._conn.commit()
            try:
                path = self.media_path(str(row["stored_name"]))
                if path.exists():
                    path.unlink()
            except (OSError, ValueError):
                pass
            return True

    async def clear(self) -> int:
        """清空全部素材，返回删除条数。"""
        async with self.lock:
            assert self._conn is not None
            count = int(
                self._conn.execute("SELECT COUNT(*) AS c FROM assets").fetchone()["c"]
                or 0
            )
            self._conn.execute("DELETE FROM assets")
            self._conn.commit()
            if self.media_dir.exists():
                shutil.rmtree(self.media_dir, ignore_errors=True)
            self.media_dir.mkdir(parents=True, exist_ok=True)
            return count

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_manifest(self) -> list[dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM assets ORDER BY created_at ASC"
        ).fetchall()
        return [self._row_to_asset(row).to_dict() for row in rows]

    def iter_media_files(self) -> Iterable[Path]:
        if not self.media_dir.exists():
            return []
        return list(self.media_dir.rglob("*"))


def write_manifest(storage: Storage, path: Path) -> None:
    """把素材清单导出成 JSON，方便备份 / 迁移。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(storage.export_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def new_asset_id() -> str:
    """生成一个短随机 id（图库前端用不上，留给未来做短链）。"""
    return uuid.uuid4().hex[:12]
