# -*- coding: utf-8 -*-
"""
MyPixelTool 核心流水线（网页界面 / 命令行共用）

流程: 文生图(Qwen-Image 2.1 Turbo) -> 图生视频(MiniMax H3 8步) -> 去背景(RMBG 2.0) -> 像素化

重要：所有中间结果都通过 custom_nodes/pixel_memory 暂存在 ComfyUI 进程内存里，
不写任何文件；只有用户在前端点「接受」时才由 webui 落盘。
"""
import io
import json
import os
import random
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # 工具根目录（ComfyUI / 环境包 / models 都在这一层）
COMFY_DIR = os.path.join(ROOT, "ComfyUI")


def _find_python():
    """定位 Python 解释器，兼容别人的目录结构。

    优先级：环境变量 MYPIXEL_PYTHON > 便携环境包（根目录下 comfyui*/python.exe）
            > .venv / venv > 当前正在运行的解释器
    """
    env = os.environ.get("MYPIXEL_PYTHON")
    if env and os.path.isfile(env):
        return env
    cands = []
    try:
        for name in sorted(os.listdir(ROOT)):
            p = os.path.join(ROOT, name)
            if not os.path.isdir(p) or not name.lower().startswith("comfyui"):
                continue
            for exe in ("python.exe", os.path.join("bin", "python3"), os.path.join("bin", "python")):
                full = os.path.join(p, exe)
                if os.path.isfile(full):
                    cands.append(full)
    except OSError:
        pass
    cands += [
        os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(ROOT, ".venv", "bin", "python3"),
        os.path.join(ROOT, "venv", "Scripts", "python.exe"),
        os.path.join(ROOT, "venv", "bin", "python3"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return sys.executable


ENV_PY = _find_python()
OUT_ROOT = os.path.join(HERE, "输出")
PORT = 8188
BASE = f"http://127.0.0.1:{PORT}"

UNET_T2I = "qwen_image_2.1_turbo_bf16.safetensors"
CLIP_T2I = "qwen3vl_8b_fp8_scaled.safetensors"
VAE_T2I = "qwen_image_2.1_vae_bf16.safetensors"
UNET_I2V = "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors"
CLIP_I2V = "qwen3vl_32b_minimax_h3_int4_convrot.safetensors"
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
RMBG = "rmbg2.0.safetensors"


# ---------------- 模型档位：装了什么就用什么 ----------------
# 极简档（只出静态图）用 int8 主模型 + w4a8 编码器，比 bf16 省 11GB 显存/体积；
# 全量档（出动画）用 bf16 高质量主模型。这里按「实际存在的文件」自动挑，
# 用户不需要改任何配置；一个都没装就退回默认名，让 ComfyUI 自己报缺模型。

MODEL_ROOTS = [os.path.join(COMFY_DIR, "models"), os.path.join(ROOT, "models")]


def has_model(rel):
    """rel 形如 'diffusion_models/xxx.safetensors'"""
    parts = rel.replace("\\", "/").split("/")
    for base in MODEL_ROOTS:
        try:
            if os.path.isfile(os.path.join(base, *parts)):
                return True
        except OSError:
            pass
    return False


def _pick(folder, names, default=None):
    for n in names:
        if has_model(f"{folder}/{n}"):
            return n
    return default or names[0]


UNET_T2I = _pick("diffusion_models",
                 ["qwen_image_2.1_turbo_bf16.safetensors",
                  "qwen_image_2.1_turbo_int8_convrot.safetensors"])
CLIP_T2I = _pick("text_encoders",
                 ["qwen3vl_8b_fp8_scaled.safetensors",
                  "qwen3vl_8b_w4a8.safetensors",
                  "qwen3vl_8b_int8_convrot.safetensors"])


def current_t2i():
    """每次现查一次（用户可能刚下载完新档位、还没重启服务）"""
    return (_pick("diffusion_models",
                  ["qwen_image_2.1_turbo_bf16.safetensors",
                   "qwen_image_2.1_turbo_int8_convrot.safetensors"]),
            _pick("text_encoders",
                  ["qwen3vl_8b_fp8_scaled.safetensors",
                   "qwen3vl_8b_w4a8.safetensors",
                   "qwen3vl_8b_int8_convrot.safetensors"]))


def video_ready():
    """出动画需要的三个权重是否齐（缺一个就没法出动画）"""
    return (has_model(f"diffusion_models/{UNET_I2V}")
            and has_model(f"text_encoders/{CLIP_I2V}")
            and has_model(f"vae/{VAE_VIDEO}"))


def model_profile():
    """给界面用的档位信息"""
    unet, clip = current_t2i()
    can_video = video_ready()
    quant = "int8" if ("int8" in unet or "w4a8" in clip) else "bf16"
    return {
        "tier": "full" if can_video else "lite",
        "canVideo": can_video,
        "canImage": has_model(f"diffusion_models/{unet}"),
        "quant": quant,
        "unetT2I": unet,
        "clipT2I": clip,
        "hasRMBG": has_model(f"background_removal/{RMBG}"),
    }

# 去背景分块：8GB 显存一次性喂 50+ 帧会爆显存，改成每块 8 帧、失败自动减半重试
RMBG_CHUNK = 8
RMBG_MIN_CHUNK = 2

NEG_T2I = ("低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，"
           "过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲")

DEFAULT_MOTION = "原地{动作}动作，循环动画，节奏清晰，游戏角色sprite动画"


def _noop(stage, msg, percent=0):
    pass


def new_key(prefix=""):
    return f"{prefix}{uuid.uuid4().hex[:14]}"


# ---------------- HTTP ----------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕过系统代理


def _open(req, timeout=30):
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read()


def http_json(path, payload=None, timeout=30):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    return json.loads(_open(req, timeout).decode("utf-8"))


def http_bytes(path, timeout=60):
    return _open(urllib.request.Request(BASE + path), timeout)


def mem_meta(key):
    try:
        return http_json(f"/pixelmem/meta?key={key}", timeout=15)
    except Exception:
        return {}


def mem_get(key, idx=0):
    return http_bytes(f"/pixelmem/get?key={key}&i={idx}")


def mem_put(data, key=None, timeout=60):
    key = key or new_key("up_")
    req = urllib.request.Request(BASE + f"/pixelmem/put?key={key}", data=data, method="POST")
    _open(req, timeout)
    return key


def mem_drop(key):
    if not key:
        return
    try:
        req = urllib.request.Request(BASE + f"/pixelmem/drop?key={key}", data=b"", method="POST")
        _open(req, timeout=10)
    except Exception:
        pass


def mem_stats():
    """内存池总占用（bytes / keys）；查不到就返回 0"""
    try:
        return http_json("/pixelmem/stats", timeout=15)
    except Exception:
        return {"keys": 0, "bytes": 0}


def mem_list():
    """内存池明细：[{key,kind,count,bytes,age}]；查不到返回空表"""
    try:
        return http_json("/pixelmem/list", timeout=15).get("items") or []
    except Exception:
        return []


def mem_clear(keep=()):
    """清空内存池（可保留指定 key，比如当前角色图），返回 {dropped, freed}"""
    try:
        return http_json("/pixelmem/clear", {"keep": list(keep)}, timeout=30)
    except Exception:
        return {"dropped": 0, "freed": 0}


def server_alive():
    try:
        http_json("/system_stats", timeout=5)
        return True
    except Exception:
        return False


def mem_nodes_ready():
    """确认内存暂存节点已加载（旧进程没有这些节点）"""
    try:
        http_json("/object_info/PixelMemSaveImages", timeout=10)
        return True
    except Exception:
        return False


def ensure_server(emit=_noop):
    if not os.path.isfile(os.path.join(COMFY_DIR, "main.py")):
        raise RuntimeError(
            f"找不到 ComfyUI（{COMFY_DIR}）。如果这是刚克隆的仓库，请先运行安装脚本："
            "Windows 双击 安装.bat，或命令行 python install.py")
    if server_alive() and mem_nodes_ready():
        return None
    if server_alive() and not mem_nodes_ready():
        raise RuntimeError("ComfyUI 正在运行旧版本（缺少内存暂存节点），请关闭 ComfyUI 窗口后重试")
    emit("启动服务", "正在启动 ComfyUI（首次约 30~60 秒）…", 0)
    import subprocess
    proc = subprocess.Popen(
        [ENV_PY, "-s", "main.py", "--port", str(PORT), "--disable-auto-launch"],
        cwd=COMFY_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(180):
        time.sleep(1)
        if server_alive():
            time.sleep(2)
            emit("启动服务", "ComfyUI 服务已就绪", 0)
            return proc
        if proc.poll() is not None:
            raise RuntimeError("ComfyUI 进程异常退出，请检查环境")
    raise RuntimeError("ComfyUI 启动超时")


def interrupt():
    try:
        http_json("/interrupt", {})
    except Exception:
        pass


def queue_workflow(graph, desc, emit=_noop, base_pct=0, span=10, is_cancelled=None, timeout_sec=3600):
    pid = http_json("/prompt", {"prompt": graph, "client_id": "mypixel"})["prompt_id"]
    emit(desc, f"任务已提交（{pid[:8]}），排队/加载中…", base_pct)
    t0 = time.time()
    last_note = -99
    while True:
        time.sleep(2)
        if is_cancelled and is_cancelled():
            interrupt()
            raise RuntimeError("已取消")
        try:
            hist = http_json(f"/history/{pid}")
        except Exception:
            hist = {}
        entry = hist.get(pid)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                detail = ""
                if msgs:
                    d = msgs[0][1]
                    detail = f"{d.get('node_type')}: {d.get('exception_message')}"
                raise RuntimeError(f"{desc} 失败 —— {detail}")
            if entry.get("outputs") is not None and status.get("completed", True):
                return entry.get("outputs") or {}
        elapsed = int(time.time() - t0)
        if elapsed - last_note >= 8:
            last_note = elapsed
            step_pct = min(span - 1, int(span * min(elapsed, 340) / 340))
            emit(desc, f"生成中… 已用 {elapsed} 秒", base_pct + step_pct)
        if elapsed > timeout_sec:
            raise RuntimeError(f"{desc} 超时（>{timeout_sec}s）")


# ---------------- 工作流 ----------------

def build_t2i(prompt, width, height, steps, seed, out_key):
    unet, clip = current_t2i()
    return {
        "clip":  {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "qwen_image", "device": "default"}},
        "vae":   {"class_type": "VAELoader", "inputs": {"vae_name": VAE_T2I}},
        "unet":  {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "pos":   {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg":   {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": NEG_T2I}},
        "latent": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "msamp": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.1}},
        "ksampler": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
            "denoise": 1.0, "model": ["msamp", 0], "positive": ["pos", 0], "negative": ["neg", 0],
            "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["ksampler", 0], "vae": ["vae", 0]}},
        "save":  {"class_type": "PixelMemSaveImages", "inputs": {"images": ["decode", 0], "key": out_key}},
    }


def snap_length(seconds):
    n = max(5, round(seconds * 24))
    n += (5 - n % 17) % 17        # 对齐模型 17k+5 帧网格
    return n


def build_i2v(prompt, width, height, length, seed, char_key, out_key):
    return {
        "unet":  {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_I2V, "weight_dtype": "default"}},
        "clip":  {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_I2V, "type": "minimax", "device": "default"}},
        "vae_v": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_VIDEO}},
        "image": {"class_type": "PixelMemImage", "inputs": {"key": char_key, "index": 0}},
        "cond":  {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["clip", 0], "vae": ["vae_v", 0], "prompt": prompt,
            "width": width, "height": height, "length": length, "first_frame": ["image", 0]}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {
            "model": ["unet", 0], "scheduler": "simple", "steps": 8, "denoise": 1.0}},
        "guider": {"class_type": "BasicGuider", "inputs": {"model": ["unet", 0], "conditioning": ["cond", 0]}},
        "advanced": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": ["cond", 1]}},
        "decode_v": {"class_type": "VAEDecode", "inputs": {"samples": ["advanced", 0], "vae": ["vae_v", 0]}},
        "save": {"class_type": "PixelMemSaveImages", "inputs": {"images": ["decode_v", 0], "key": out_key}},
    }


def build_rmbg(key_in, out_key, offset=0, limit=0, append=False):
    return {
        "model": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": RMBG}},
        "load":  {"class_type": "PixelMemLoadImages", "inputs": {"key": key_in, "limit": limit, "offset": offset}},
        "rmbg":  {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["model", 0], "image": ["load", 0]}},
        "save":  {"class_type": "PixelMemSaveMasks", "inputs": {"mask": ["rmbg", 0], "key": out_key, "append": append}},
    }


def free_vram():
    """让 ComfyUI 把已加载的模型从显存卸载（视频模型很占显存，去背景前必须先腾出来）"""
    try:
        http_json("/free", {"unload_models": True, "free_memory": True}, timeout=30)
        time.sleep(1.0)
        return True
    except Exception:
        return False


# ---------------- 图像处理（全内存） ----------------

def pixelize_bytes(frame_png, mask_png, pixel_size, colors=32):
    """原始帧 + 遮罩 -> 透明像素图 PNG 字节"""
    from PIL import Image
    img = Image.open(io.BytesIO(frame_png)).convert("RGB")
    mask = Image.open(io.BytesIO(mask_png)).convert("L").resize(img.size, Image.BILINEAR)

    w, h = img.size
    scale = pixel_size / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    small = img.resize((nw, nh), Image.BOX).convert("RGBA")
    small.putalpha(mask.resize((nw, nh), Image.BOX))
    small = small.quantize(colors=colors, method=Image.Quantize.FASTOCTREE).convert("RGBA")
    small.putalpha(small.getchannel("A").point(lambda v: 255 if v >= 128 else 0))

    factor = max(1, pixel_size // max(nw, nh))
    out = small.resize((nw * factor, nh * factor), Image.NEAREST)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def matte_bytes(frame_png, mask_png):
    """原始帧 + 遮罩 -> 全分辨率透明 PNG（不像素化，视频多清楚就多清楚）"""
    from PIL import Image
    img = Image.open(io.BytesIO(frame_png)).convert("RGBA")
    mask = Image.open(io.BytesIO(mask_png)).convert("L").resize(img.size, Image.BILINEAR)
    img.putalpha(mask)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_preview(png_bytes, box=160):
    """给前端列表用的小尺寸预览（仍是内存字节）"""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    im.thumbnail((box, box), Image.BOX)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def encode_mp4(frames_png, fps=24):
    """把 PNG 帧序列编码成 mp4 字节（用于「保留中间视频」）"""
    import av
    from PIL import Image
    if not frames_png:
        return b""
    first = Image.open(io.BytesIO(frames_png[0])).convert("RGB")
    w, h = first.size
    w, h = w - (w % 2), h - (h % 2)
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mp4") as out:
        st = out.add_stream("libx264", rate=fps, width=w, height=h, pix_fmt="yuv420p",
                            options={"crf": "20", "preset": "veryfast"})
        for png in frames_png:
            im = Image.open(io.BytesIO(png)).convert("RGB").resize((w, h), Image.BILINEAR)
            for pkt in st.encode(av.VideoFrame.from_image(im)):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)
    return buf.getvalue()


# ---------------- 磁盘（仅用户接受后调用） ----------------

def safe_name(name):
    bad = '\\/:*?"<>|'
    return "".join(("_" if c in bad else c) for c in name).strip() or "未命名"


def load_manifest(name, root=None):
    """root：自定义输出根目录（挂机模式可选盘符），默认 OUT_ROOT"""
    base = root or OUT_ROOT
    name = safe_name(name)
    p = os.path.join(base, name, "资产.json")
    data = {"name": name, "prompt": "", "actions": {}, "charImage": ""}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
    d = os.path.join(base, name)
    if os.path.isdir(d):
        for a in sorted(os.listdir(d)):
            fd = os.path.join(d, a, "像素帧")
            if os.path.isdir(fd) and a not in data["actions"]:
                n = len([f for f in os.listdir(fd) if f.endswith(".png")])
                data["actions"][a] = {"frames": n, "pixel": 64, "duration": 0, "created": ""}
    return data


def save_manifest(name, data, root=None):
    base = root or OUT_ROOT
    name = safe_name(name)
    os.makedirs(os.path.join(base, name), exist_ok=True)
    with open(os.path.join(base, name, "资产.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_char_image(name, png_bytes, root=None):
    base = root or OUT_ROOT
    name = safe_name(name)
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "角色图.png"), "wb") as f:
        f.write(png_bytes)
    man = load_manifest(name, root)
    man["charImage"] = "角色图.png"
    save_manifest(name, man, root)


PIXEL_IMAGE = "像素图"        # 静态像素图的伪动作名（目录里存 像素图.png）
PIXEL_DIR = "像素帧"          # 像素化后的帧目录
RAW_DIR = "原画帧"            # 不像素化、直接抽视频帧的目录


def frame_dir_name(pixel):
    return RAW_DIR if int(pixel or 0) <= 0 else PIXEL_DIR


def action_frames_dir(name, action, root=None, man=None):
    """按 manifest 记录判断这个动作的帧在哪个目录（像素帧 / 原画帧）"""
    base = root or OUT_ROOT
    man = man if man is not None else load_manifest(name, base)
    meta = (man.get("actions") or {}).get(action) or {}
    sub = RAW_DIR if meta.get("raw") else PIXEL_DIR
    d = os.path.join(base, safe_name(name), safe_name(action), sub)
    if os.path.isdir(d):
        return d
    alt = os.path.join(base, safe_name(name), safe_name(action), PIXEL_DIR)
    return alt if os.path.isdir(alt) else d


def save_pixel_image(name, png_bytes, pixel, root=None):
    """保存静态像素图到 <资产>/像素图.png，并在 manifest 里登记"""
    base = root or OUT_ROOT
    name = safe_name(name)
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, PIXEL_IMAGE + ".png"), "wb") as f:
        f.write(png_bytes)
    man = load_manifest(name, root)
    man["pixelImage"] = {"pixel": int(pixel), "created": time.strftime("%Y-%m-%d %H:%M")}
    save_manifest(name, man, root)
    return os.path.join(d, PIXEL_IMAGE + ".png")


def load_pixel_image(name, root=None):
    """读静态像素图；没有返回 None"""
    p = os.path.join(root or OUT_ROOT, safe_name(name), PIXEL_IMAGE + ".png")
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return f.read()


def pixelize_image(char_key, pixel, emit=_noop, is_cancelled=None, colors=32):
    """把内存里的角色图抠图 + 像素化，返回像素图 PNG 字节（不动原角色图）"""
    if not char_key:
        raise RuntimeError("没有角色图")
    ensure_server(emit)
    emit("准备", "环境就绪", 1)
    mask_key = new_key("pmask_")
    try:
        emit("去背景", "抠图中…", 10)
        free_vram()
        queue_workflow(build_rmbg(char_key, mask_key), "去背景", emit, 10, 75, is_cancelled)
        if is_cancelled and is_cancelled():
            raise RuntimeError("已取消")
        if int((mem_meta(mask_key) or {}).get("count") or 0) <= 0:
            raise RuntimeError("去背景没有返回遮罩")
        mask_bytes = mem_get(mask_key, 0)
        emit("像素化", "像素化中…", 88)
        return pixelize_bytes(mem_get(char_key, 0), mask_bytes, int(pixel), colors)
    finally:
        try:
            mem_drop(mask_key)
        except Exception:
            pass


def save_action(name, action, frames, pixel, duration, video_bytes=None, root=None):
    """用户接受（或挂机模式）时才会调用；root 可指定输出根目录

    pixel > 0  → 存「像素帧」目录；pixel <= 0 → 存「原画帧」目录（未像素化的视频原帧）
    """
    base = root or OUT_ROOT
    name = safe_name(name)
    act = safe_name(action)
    act_dir = os.path.join(base, name, act)
    sub = frame_dir_name(pixel)
    final_dir = os.path.join(act_dir, sub)
    os.makedirs(final_dir, exist_ok=True)
    # 两种模式切换重做时，清掉另一个目录里的旧帧，避免残留混淆
    for other in (PIXEL_DIR, RAW_DIR):
        od = os.path.join(act_dir, other)
        if od != final_dir and os.path.isdir(od):
            for old in os.listdir(od):
                if old.endswith(".png"):
                    os.remove(os.path.join(od, old))
            try:
                os.rmdir(od)
            except OSError:
                pass
    for old in os.listdir(final_dir):
        if old.endswith(".png"):
            os.remove(os.path.join(final_dir, old))
    for i, b in enumerate(frames):
        with open(os.path.join(final_dir, f"{i:04d}.png"), "wb") as f:
            f.write(b)
    if video_bytes:
        with open(os.path.join(act_dir, f"{act}.mp4"), "wb") as f:
            f.write(video_bytes)
    meta = {"frames": len(frames), "pixel": int(pixel or 0), "duration": duration,
            "created": time.strftime("%Y-%m-%d %H:%M"), "dir": sub}
    if int(pixel or 0) <= 0:
        meta["raw"] = True
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(frames[0]))
            meta["size"] = [im.width, im.height]
        except Exception:
            pass
    man = load_manifest(name, root)
    man.setdefault("actions", {})[action] = meta
    save_manifest(name, man, root)
    return act_dir


# ---------------- 核心动作 ----------------

def _parse_size(s, default="1328x1328"):
    try:
        w, h = [int(x) for x in str(s or default).lower().split("x")]
    except Exception:
        w, h = [int(x) for x in default.split("x")]
    return w, h


_vram_memo = {}


def gpu_vram_gb():
    """查一次显存并缓存；查不到返回 0（当成未知，不做限制）"""
    if "v" in _vram_memo:
        return _vram_memo["v"]
    v = 0
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, timeout=10).stdout.decode("utf-8", "ignore")
        lines = [x for x in out.strip().splitlines() if x.strip()]
        if lines:
            v = round(float(lines[0].strip()) / 1024, 1)
    except Exception:
        pass
    _vram_memo["v"] = v
    return v


def cap_size_for_vram(w, h):
    """显存不足时把分辨率压到安全值。像素图最终只有 32~128px，降分辨率几乎不影响成品"""
    vram = gpu_vram_gb()
    if not vram:
        return w, h, ""
    cap = 896 if vram < 6 else (1024 if vram < 8 else None)
    if not cap or max(w, h) <= cap:
        return w, h, ""
    scale = cap / max(w, h)
    nw, nh = _snap32(w * scale), _snap32(h * scale)
    return nw, nh, f"（显存 {vram}GB，已自动把分辨率从 {w}x{h} 降到 {nw}x{nh}）"


def _snap32(v, lo=256):
    return max(lo, round(v / 32) * 32)


def generate_candidates(params, count, emit=_noop, is_cancelled=None):
    """批量出候选角色图，全部只在内存。返回 [{"key","png","seed"}]"""
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("请先填写角色提示词")
    _u, _c = current_t2i()
    if not has_model(f"diffusion_models/{_u}") or not has_model(f"text_encoders/{_c}"):
        raise RuntimeError("还没装文生图模型；请先双击「下载模型.bat」把模型下齐")
    ensure_server(emit)
    emit("准备", "环境就绪", 1)
    w, h = _parse_size(params.get("size"), "1328x1328")
    w, h, _cap_note = cap_size_for_vram(w, h)
    if _cap_note:
        emit("准备", _cap_note, 1)
    steps = int(params.get("steps", 8))
    count = max(1, min(12, int(count)))

    out = []
    try:
        for i in range(count):
            if is_cancelled and is_cancelled():
                raise RuntimeError("已取消")
            base = int(97 * i / count)
            span = max(2, int(97 / count))
            key = new_key("cand_")
            seed = random.randint(0, 2 ** 48)
            emit("文生图", f"候选图 {i+1}/{count} 生成中…", base)
            queue_workflow(build_t2i(prompt, w, h, steps, seed, key),
                           f"文生图 {i+1}/{count}", emit, base, span, is_cancelled)
            png = mem_get(key, 0)
            out.append({"key": key, "png": png, "seed": seed})
            emit("文生图", f"候选图 {i+1}/{count} 完成", base + span)
    except Exception:
        # 半途失败/取消：已经生成的候选图也要归还内存池，不能白占
        for it in out:
            mem_drop(it["key"])
        raise
    emit("完成", f"已生成 {len(out)} 张候选图（还在内存里，未落盘）", 100)
    return out


def render_actions(params, char_key, actions, emit=_noop, is_cancelled=None):
    """基于内存中的角色图生成动作，像素帧全部留在内存。返回 list[dict]"""
    if not char_key:
        raise RuntimeError("没有角色图，请先生成或选择一张")
    if not video_ready():
        raise RuntimeError("当前是极简档，没装动画模型；要出动画请运行「下载模型.bat」选全量档")
    ensure_server(emit)
    emit("准备", "环境就绪", 1)

    pixelize = params.get("pixelize", True)
    if isinstance(pixelize, str):
        pixelize = pixelize.strip().lower() not in ("0", "false", "no", "off", "")
    pixel = int(params.get("pixel", 64)) if pixelize else 0
    duration = float(params.get("duration", 2.0))
    vw, vh = _parse_size(params.get("video_size"), "864x480")
    vw, vh = _snap32(vw), _snap32(vh)          # 必须是 32 的倍数
    length = snap_length(duration)
    motion_tpl = str(params.get("motion") or DEFAULT_MOTION)
    keep_video = bool(params.get("keep_video"))
    actions = [a for a in (actions or []) if str(a).strip()]
    if not actions:
        raise RuntimeError("请至少选择一个动作")

    results = []
    total = len(actions)
    for idx, action in enumerate(actions):
        if is_cancelled and is_cancelled():
            raise RuntimeError("已取消")
        base = int(96 * idx / total)
        span = max(4, int(96 / total))
        motion = motion_tpl.replace("{动作}", action)

        vid_key = new_key("vid_")
        mask_key = new_key("mask_")
        try:
            emit("图生视频", f"【{action}】生成视频（{idx+1}/{total}）", base)
            queue_workflow(build_i2v(motion, vw, vh, length, random.randint(0, 2 ** 48), char_key, vid_key),
                           "图生视频", emit, base, int(span * 0.72), is_cancelled)

            meta = mem_meta(vid_key)
            n = int(meta.get("count") or 0)
            if n <= 0:
                raise RuntimeError("视频生成后内存中没有帧数据")

            emit("去背景", f"【{action}】抠图中…（{n} 帧，分 {(n + RMBG_CHUNK - 1) // RMBG_CHUNK} 块）", base + int(span * 0.74))
            free_vram()          # 先把视频模型从显存卸载，否则 RMBG 抢不到空间
            chunk = RMBG_CHUNK
            off = 0
            while off < n:
                if is_cancelled and is_cancelled():
                    raise RuntimeError("已取消")
                take = min(chunk, n - off)
                try:
                    queue_workflow(build_rmbg(vid_key, mask_key, offset=off, limit=take, append=(off > 0)),
                                   "去背景", emit,
                                   base + int(span * 0.74),
                                   max(2, int(span * 0.18 * take / max(1, n))),
                                   is_cancelled)
                except RuntimeError as e:
                    if "memory" in str(e).lower() and chunk > RMBG_MIN_CHUNK:
                        chunk = max(RMBG_MIN_CHUNK, chunk // 2)
                        emit("去背景", f"显存不足，改为每块 {chunk} 帧重试…", base + int(span * 0.74))
                        free_vram()
                        continue
                    raise
                off += take

            mmeta = mem_meta(mask_key)
            m = min(n, int(mmeta.get("count") or 0))
            if m <= 0:
                raise RuntimeError("去背景没有返回遮罩")

            frames = []
            stage = "像素化" if pixelize else "抽帧"
            for i in range(m):
                if is_cancelled and is_cancelled():
                    raise RuntimeError("已取消")
                fbytes = mem_get(vid_key, i)
                mask_bytes = mem_get(mask_key, i)
                if pixelize:
                    frames.append(pixelize_bytes(fbytes, mask_bytes, pixel))
                else:
                    frames.append(matte_bytes(fbytes, mask_bytes))
                if i % 6 == 0:
                    emit(stage, f"【{action}】处理帧 {i+1}/{m}",
                         base + int(span * (0.92 + 0.07 * i / max(1, m))))

            video_bytes = None
            if keep_video:
                try:
                    raw = [mem_get(vid_key, i) for i in range(n)]
                    video_bytes = encode_mp4(raw, fps=24)
                except Exception as e:
                    emit("提示", f"视频编码跳过：{e}", base + span - 1)
        finally:
            # 无论成功、失败还是取消，视频帧和遮罩都必须归还内存池，
            # 否则一次中断就白占几百 MB（这就是“内存一直被占”的元凶）
            mem_drop(vid_key)
            mem_drop(mask_key)

        # 只留第一帧的预览图给前端做缩略图，其余保持原始字节
        preview = frames[0] if frames else b""
        results.append({
            "action": action,
            "frames": frames,
            "pixel": pixel,
            "duration": duration,
            "video": video_bytes,
            "preview": preview,
            "raw": not pixelize,
            "size": [vw, vh],
        })
        emit("完成", f"【{action}】完成，共 {m} 帧（待你确认后才会存盘）", base + span)

    return results


def run_pipeline(params, emit=_noop, is_cancelled=None):
    """命令行兼容入口：生成后直接保存到磁盘"""
    name = safe_name(params["name"])
    actions = params.get("actions") or ["站立"]
    ensure_server(emit)

    char_key = params.get("char_key")
    if not char_key:
        char_png = os.path.join(OUT_ROOT, name, "角色图.png")
        if params.get("image"):
            with open(params["image"], "rb") as f:
                char_key = mem_put(f.read())
            save_char_image(name, mem_get_only(char_key))
        elif params.get("reuse_char") and os.path.exists(char_png):
            with open(char_png, "rb") as f:
                char_key = mem_put(f.read())
        else:
            cands = generate_candidates(params, 1, emit, is_cancelled)
            char_key = cands[0]["key"]
            save_char_image(name, cands[0]["png"])

    results = render_actions(params, char_key, actions, emit, is_cancelled)
    try:
        for r in results:
            save_action(name, r["action"], r["frames"], r["pixel"], r["duration"], r.get("video"))
    finally:
        mem_drop(char_key)
    emit("完成", "全部动作完成", 100)
    return {"name": name, "dir": os.path.join(OUT_ROOT, name), "actions": actions}


def mem_get_only(key):
    return mem_get(key, 0)


# ---------------- 命令行入口 ----------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="MyPixelTool 像素动画流水线")
    ap.add_argument("--name", required=True)
    ap.add_argument("--prompt")
    ap.add_argument("--image")
    ap.add_argument("--reuse-char", action="store_true", help="复用该资产已生成的角色图")
    ap.add_argument("--actions", nargs="+", default=["站立"])
    ap.add_argument("--motion", default=DEFAULT_MOTION)
    ap.add_argument("--size", default="1328x1328")
    ap.add_argument("--video-size", default="864x480")
    ap.add_argument("--duration", type=float, default=2.0)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--pixel", type=int, default=64)
    ap.add_argument("--no-pixelize", action="store_true",
                    help="不像素化：直接抽视频原帧，输出全分辨率透明 PNG（更快、更清晰）")
    ap.add_argument("--keep-video", action="store_true")
    args = ap.parse_args()

    def cli_emit(stage, msg, percent=0):
        print(f"[{percent:3d}%] {stage}: {msg}", flush=True)

    params = vars(args)
    if params.pop("no_pixelize", False):
        params["pixelize"] = False
    run_pipeline(params, cli_emit)
    print("\n完成！输出目录:", os.path.join(OUT_ROOT, safe_name(args.name)))


if __name__ == "__main__":
    main()
