"""T007 背景上传：只写 static/bg，独占建文件，失败不碰界面设置。"""
import io
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from email import policy
from email.parser import BytesParser
from pathlib import Path

from fastapi import UploadFile

from . import settings as ST

MAX_BYTES = 20 * 1024 * 1024
# multipart 分隔线与头部开销的余量：流式上限多给 64KB，真正的 20MB 仍按文件字节判（save_upload 里再核一次）
MULTIPART_SLACK = 64 * 1024


async def read_upload(request) -> UploadFile:
    """没有 python-multipart 时以标准库解析有界表单，不向工作区外写临时文件。"""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;"):
        raise ValueError("请用文件选择框上传一张背景图")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES + MULTIPART_SLACK:
            raise ValueError("背景图不能超过 20MB，请换一张小一点的图")
    msg = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + data)
    parts = list(msg.iter_parts()) if msg.is_multipart() else []
    if msg.defects or len(parts) != 1 or parts[0].get_param("name", header="content-disposition") != "file":
        raise ValueError("每次请选择一张背景图，上传表单读不出来")
    part = parts[0]
    payload = part.get_payload(decode=True)
    if part.defects or not isinstance(payload, bytes):
        raise ValueError("背景图上传不完整，请重新选择")
    return UploadFile(file=io.BytesIO(payload), filename=part.get_filename() or "")


def image_matches(data: bytes, ext: str) -> bool:
    """不信后缀与 MIME：检查图片容器，PNG 逐块核 CRC，SVG 禁止主动内容。"""
    if ext == ".png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        pos, kinds = 8, []
        while pos + 12 <= len(data):
            size = int.from_bytes(data[pos:pos + 4], "big")
            end = pos + size + 12
            if end > len(data):
                return False
            kind = data[pos + 4:pos + 8]
            if zlib.crc32(data[pos + 4:end - 4]) != int.from_bytes(data[end - 4:end], "big"):
                return False
            if not kinds and (kind != b"IHDR" or size != 13 or not all(struct.unpack(">II", data[pos + 8:pos + 16]))):
                return False
            kinds.append(kind)
            pos = end
            if kind == b"IEND":
                return size == 0 and b"IDAT" in kinds and pos == len(data)
        return False
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9") and len(data) > 32
    if ext == ".gif":
        return data[:6] in (b"GIF87a", b"GIF89a") and len(data) > 13 and data.endswith(b";")
    if ext == ".webp":
        return (len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
                and data[12:16] in (b"VP8 ", b"VP8L", b"VP8X")
                and int.from_bytes(data[4:8], "little") + 8 == len(data))
    if ext == ".avif":
        return len(data) >= 24 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis")
    if ext == ".svg":
        try:
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                return False
            root = ET.fromstring(data)
            if root.tag.split("}")[-1] != "svg":
                return False
            for el in root.iter():
                if el.tag.split("}")[-1].lower() in ("script", "foreignobject"):
                    return False
                for key, value in el.attrib.items():
                    local = key.split("}")[-1].lower()
                    if local.startswith("on") or (local == "href" and not value.startswith("#")):
                        return False
                for value in [el.text or "", *el.attrib.values()]:
                    if "@import" in value.lower():
                        return False
                    refs = re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", value, re.I)
                    if any(not ref.startswith("#") for ref in refs):
                        return False
            return True
        except (ValueError, ET.ParseError):
            return False
    return False


def clean_name(filename: str) -> str:
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    ext = Path(name).suffix.lower()
    if ext not in ST.BG_EXTS:
        raise ValueError("请选择 PNG、JPG、WebP、GIF、SVG 或 AVIF 背景图")
    stem = Path(name).stem[:100].strip(" .") or "background"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if stem.split(".")[0].upper() in reserved:
        stem = "_" + stem
    return stem + ext


def save_upload(file: UploadFile) -> str:
    name = clean_name(file.filename or "")
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("背景图不能超过 20MB，请换一张小一点的图")
    if not image_matches(data, Path(name).suffix):
        raise ValueError("这张图读不出来，内容与图片后缀不符或文件已损坏")
    ST.BG_DIR.mkdir(parents=True, exist_ok=True)
    base = ST.BG_DIR.resolve()
    if ST.BG_DIR.is_symlink() or ST.BG_DIR.is_junction():
        raise ValueError("背景文件夹不能是链接，请恢复 static/bg 文件夹")
    stem, ext = Path(name).stem, Path(name).suffix
    index = 0
    while True:
        candidate = base / (name if index == 0 else f"{stem}_{index}{ext}")
        try:
            with candidate.open("xb") as target:
                try:
                    target.write(data)
                except OSError:
                    # 只清本次独占创建的半截文件，已有图片永不覆盖。
                    target.close()
                    candidate.unlink()
                    raise
            return candidate.name
        except FileExistsError:
            index += 1
        except OSError as e:
            raise ValueError("背景图存不进磁盘，请检查 static/bg 的空间和写入权限") from e
