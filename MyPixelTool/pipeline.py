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
    return {
        "clip":  {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_T2I, "type": "qwen_image", "device": "default"}},
        "vae":   {"class_type": "VAELoader", "inputs": {"vae_name": VAE_T2I}},
        "unet":  {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_T2I, "weight_dtype": "default"}},
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


def load_manifest(name):
    p = os.path.join(OUT_ROOT, name, "资产.json")
    data = {"name": name, "prompt": "", "actions": {}, "charImage": ""}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
    d = os.path.join(OUT_ROOT, name)
    if os.path.isdir(d):
        for a in sorted(os.listdir(d)):
            fd = os.path.join(d, a, "像素帧")
            if os.path.isdir(fd) and a not in data["actions"]:
                n = len([f for f in os.listdir(fd) if f.endswith(".png")])
                data["actions"][a] = {"frames": n, "pixel": 64, "duration": 0, "created": ""}
    return data


def save_manifest(name, data):
    os.makedirs(os.path.join(OUT_ROOT, name), exist_ok=True)
    with open(os.path.join(OUT_ROOT, name, "资产.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_char_image(name, png_bytes):
    d = os.path.join(OUT_ROOT, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "角色图.png"), "wb") as f:
        f.write(png_bytes)
    man = load_manifest(name)
    man["charImage"] = "角色图.png"
    save_manifest(name, man)


def save_action(name, action, frames, pixel, duration, video_bytes=None):
    """用户接受后才会调用"""
    act_dir = os.path.join(OUT_ROOT, name, action)
    final_dir = os.path.join(act_dir, "像素帧")
    os.makedirs(final_dir, exist_ok=True)
    for old in os.listdir(final_dir):
        if old.endswith(".png"):
            os.remove(os.path.join(final_dir, old))
    for i, b in enumerate(frames):
        with open(os.path.join(final_dir, f"{i:04d}.png"), "wb") as f:
            f.write(b)
    if video_bytes:
        with open(os.path.join(act_dir, f"{action}.mp4"), "wb") as f:
            f.write(video_bytes)
    man = load_manifest(name)
    man["actions"][action] = {"frames": len(frames), "pixel": pixel, "duration": duration,
                              "created": time.strftime("%Y-%m-%d %H:%M")}
    save_manifest(name, man)
    return act_dir


# ---------------- 核心动作 ----------------

def _parse_size(s, default="1328x1328"):
    try:
        w, h = [int(x) for x in str(s or default).lower().split("x")]
    except Exception:
        w, h = [int(x) for x in default.split("x")]
    return w, h


def _snap32(v, lo=256):
    return max(lo, round(v / 32) * 32)


def generate_candidates(params, count, emit=_noop, is_cancelled=None):
    """批量出候选角色图，全部只在内存。返回 [{"key","png","seed"}]"""
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("请先填写角色提示词")
    ensure_server(emit)
    emit("准备", "环境就绪", 1)
    w, h = _parse_size(params.get("size"), "1328x1328")
    steps = int(params.get("steps", 8))
    count = max(1, min(12, int(count)))

    out = []
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
    emit("完成", f"已生成 {len(out)} 张候选图（还在内存里，未落盘）", 100)
    return out


def render_actions(params, char_key, actions, emit=_noop, is_cancelled=None):
    """基于内存中的角色图生成动作，像素帧全部留在内存。返回 list[dict]"""
    if not char_key:
        raise RuntimeError("没有角色图，请先生成或选择一张")
    ensure_server(emit)
    emit("准备", "环境就绪", 1)

    pixel = int(params.get("pixel", 64))
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
        emit("图生视频", f"【{action}】生成视频（{idx+1}/{total}）", base)
        queue_workflow(build_i2v(motion, vw, vh, length, random.randint(0, 2 ** 48), char_key, vid_key),
                       "图生视频", emit, base, int(span * 0.72), is_cancelled)

        meta = mem_meta(vid_key)
        n = int(meta.get("count") or 0)
        if n <= 0:
            raise RuntimeError("视频生成后内存中没有帧数据")

        mask_key = new_key("mask_")
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
        for i in range(m):
            if is_cancelled and is_cancelled():
                raise RuntimeError("已取消")
            fbytes = mem_get(vid_key, i)
            mask_bytes = mem_get(mask_key, i)
            frames.append(pixelize_bytes(fbytes, mask_bytes, pixel))
            if i % 6 == 0:
                emit("像素化", f"【{action}】处理帧 {i+1}/{m}",
                     base + int(span * (0.92 + 0.07 * i / max(1, m))))

        video_bytes = None
        if keep_video:
            try:
                raw = [mem_get(vid_key, i) for i in range(n)]
                video_bytes = encode_mp4(raw, fps=24)
            except Exception as e:
                emit("提示", f"视频编码跳过：{e}", base + span - 1)

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
    for r in results:
        save_action(name, r["action"], r["frames"], r["pixel"], r["duration"], r.get("video"))
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
    ap.add_argument("--keep-video", action="store_true")
    args = ap.parse_args()

    def cli_emit(stage, msg, percent=0):
        print(f"[{percent:3d}%] {stage}: {msg}", flush=True)

    run_pipeline(vars(args), cli_emit)
    print("\n完成！输出目录:", os.path.join(OUT_ROOT, safe_name(args.name)))


if __name__ == "__main__":
    main()
