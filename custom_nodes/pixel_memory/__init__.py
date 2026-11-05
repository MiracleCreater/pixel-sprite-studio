# -*- coding: utf-8 -*-
"""
PixelSmith 内存暂存节点（MyPixelTool 配套）

作用：把 ComfyUI 生成出来的图像/遮罩先放在进程内存里，不写任何文件。
用户在前端确认接受之后，才由 webui 落盘。这样反复试错不会磨损硬盘。

对外 HTTP 接口（webui 通过它们取数据）：
  GET  /pixelmem/meta?key=K          -> {"kind","count"}
  GET  /pixelmem/get?key=K&i=N       -> 该帧 PNG 原始字节
  POST /pixelmem/put?key=K  (body=PNG) -> 外部图片写入内存（拖拽上传用）
  POST /pixelmem/drop?key=K          -> 主动释放
"""
import io
import threading
import time
import uuid

import numpy as np
import torch
from aiohttp import web
from PIL import Image

from server import PromptServer

MAX_TOTAL_BYTES = 3 * 1024 ** 3      # 内存池上限 3GB
TTL_SEC = 3 * 3600                   # 3 小时未访问自动回收

_STORE = {}
_LOCK = threading.Lock()
_TOTAL = 0


def _purge_locked(force=False):
    """调用前必须已持有 _LOCK"""
    global _TOTAL
    now = time.time()
    for k in [k for k, v in _STORE.items() if now - v["ts"] > TTL_SEC]:
        _TOTAL -= _STORE[k]["size"]
        del _STORE[k]
    while force and _TOTAL > MAX_TOTAL_BYTES and _STORE:
        oldest = min(_STORE, key=lambda k: _STORE[k]["ts"])
        _TOTAL -= _STORE[oldest]["size"]
        del _STORE[oldest]


def _store_blob(key, kind, items):
    global _TOTAL
    key = key or ("mem_" + uuid.uuid4().hex[:12])
    size = sum(len(b) for b in items)
    with _LOCK:
        old = _STORE.pop(key, None)
        if old:
            _TOTAL -= old["size"]
        _STORE[key] = {"kind": kind, "items": items, "ts": time.time(), "size": size}
        _TOTAL += size
        _purge_locked(force=True)
    return key, len(items)


def _append_blob(key, items):
    """把新的条目追加到已有 key（分块去背景时逐块累加遮罩）"""
    global _TOTAL
    add = sum(len(b) for b in items)
    with _LOCK:
        v = _STORE.get(key)
        if v is None:
            _STORE[key] = {"kind": "mask", "items": list(items), "ts": time.time(), "size": add}
            _TOTAL += add
            _purge_locked(force=True)
            return key, len(_STORE[key]["items"])
        v["items"].extend(items)
        v["size"] += add
        v["ts"] = time.time()
        _TOTAL += add
        _purge_locked(force=True)
        return key, len(v["items"])


def _get_blob(key):
    with _LOCK:
        v = _STORE.get(key)
        if v is not None:
            v["ts"] = time.time()
        return v


def _drop_blob(key):
    global _TOTAL
    with _LOCK:
        v = _STORE.pop(key, None)
        if v:
            _TOTAL -= v["size"]


def _img_png(t):
    arr = (t.detach().float().clamp(0, 1) * 255.0).round().to(torch.uint8).cpu().numpy()
    buf = io.BytesIO()
    Image.fromarray(arr).convert("RGB").save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def _mask_png(m):
    arr = (m.detach().float().clamp(0, 1) * 255.0).round().to(torch.uint8).cpu().numpy()
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def _png_tensor(b):
    im = Image.open(io.BytesIO(b))
    if im.mode != "RGB":
        im = im.convert("RGB")
    a = np.asarray(im).astype(np.float32) / 255.0
    return torch.from_numpy(a)[None, ...]


# ---------------- 节点 ----------------

class PixelMemSaveImages:
    """把 IMAGE 批次编码成 PNG 存进内存，不落盘"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",), "key": ("STRING", {"default": ""})}}

    RETURN_TYPES = ()
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "pixel/memory"

    def run(self, images, key=""):
        n = images.shape[0]
        items = [_img_png(images[i]) for i in range(n)]
        key, n = _store_blob(key, "image", items)
        return {"ui": {"pixel_keys": [key], "pixel_count": [n]}}


class PixelMemSaveMasks:
    """把 MASK 批次存进内存（灰度 PNG）

    append=True 时追加到已有 key，供「分块去背景」把多批遮罩拼成完整序列。
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mask": ("MASK",), "key": ("STRING", {"default": ""})},
                "optional": {"append": ("BOOLEAN", {"default": False})}}

    RETURN_TYPES = ()
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "pixel/memory"

    def run(self, mask, key="", append=False):
        n = mask.shape[0]
        items = [_mask_png(mask[i]) for i in range(n)]
        if append and key:
            key, n = _append_blob(key, items)
        else:
            key, n = _store_blob(key, "mask", items)
        return {"ui": {"pixel_keys": [key], "pixel_count": [n]}}


class PixelMemLoadImages:
    """从内存取出图像，供去背景等后续节点使用

    limit=0 表示取到末尾；offset 用于分块处理（一次只喂几十帧，避免显存爆掉）。
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"key": ("STRING", {"default": ""}),
                             "limit": ("INT", {"default": 0, "min": 0, "max": 4096})},
                "optional": {"offset": ("INT", {"default": 0, "min": 0, "max": 4096})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "pixel/memory"

    def run(self, key, limit=0, offset=0):
        v = _get_blob(key)
        if not v or not v["items"]:
            raise RuntimeError(f"内存中没有 key={key} 的数据（可能已被回收）")
        items = v["items"][offset:]
        if limit:
            items = items[:limit]
        if not items:
            raise RuntimeError(f"key={key} 的 offset={offset} 超出范围")
        tensors = [_png_tensor(b) for b in items]
        h, w = tensors[0].shape[1:3]
        fixed = [t for t in tensors if t.shape[1] == h and t.shape[2] == w]
        if not fixed:
            raise RuntimeError("内存中的帧尺寸不一致")
        return (torch.cat(fixed, dim=0),)


class PixelMemImage:
    """从内存取单张图（用作图生视频的首帧）"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"key": ("STRING", {"default": ""}),
                             "index": ("INT", {"default": 0, "min": 0, "max": 4096})}}

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "run"
    CATEGORY = "pixel/memory"

    def run(self, key, index=0):
        v = _get_blob(key)
        if not v or not v["items"]:
            raise RuntimeError(f"内存中没有 key={key} 的数据（可能已被回收）")
        idx = min(max(0, index), len(v["items"]) - 1)
        im = Image.open(io.BytesIO(v["items"][idx]))
        mask = torch.zeros((1, im.size[1], im.size[0]), dtype=torch.float32)
        return (_png_tensor(v["items"][idx]), mask)


NODE_CLASS_MAPPINGS = {
    "PixelMemSaveImages": PixelMemSaveImages,
    "PixelMemSaveMasks": PixelMemSaveMasks,
    "PixelMemLoadImages": PixelMemLoadImages,
    "PixelMemImage": PixelMemImage,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PixelMemSaveImages": "内存暂存 · 图像",
    "PixelMemSaveMasks": "内存暂存 · 遮罩",
    "PixelMemLoadImages": "内存读取 · 批量图像",
    "PixelMemImage": "内存读取 · 单张图像",
}

# ---------------- HTTP 接口 ----------------

routes = PromptServer.instance.routes


@routes.get("/pixelmem/meta")
async def _api_meta(request):
    key = request.query.get("key", "")
    v = _get_blob(key)
    if not v:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"key": key, "kind": v["kind"], "count": len(v["items"])})


@routes.get("/pixelmem/get")
async def _api_get(request):
    key = request.query.get("key", "")
    try:
        idx = int(request.query.get("i", 0))
    except Exception:
        idx = 0
    v = _get_blob(key)
    if not v or idx < 0 or idx >= len(v["items"]):
        return web.json_response({"error": "not found"}, status=404)
    return web.Response(body=v["items"][idx], content_type="image/png",
                        headers={"Cache-Control": "no-store"})


@routes.post("/pixelmem/put")
async def _api_put(request):
    key = request.query.get("key", "")
    data = await request.read()
    if len(data) < 64:
        return web.json_response({"error": "empty"}, status=400)
    key, n = _store_blob(key, "image", [data])
    return web.json_response({"ok": True, "key": key, "count": n})


@routes.post("/pixelmem/drop")
async def _api_drop(request):
    key = request.query.get("key", "")
    _drop_blob(key)
    return web.json_response({"ok": True})


@routes.get("/pixelmem/stats")
async def _api_stats(request):
    with _LOCK:
        return web.json_response({"keys": len(_STORE), "bytes": _TOTAL})
