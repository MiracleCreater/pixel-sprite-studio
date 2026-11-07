# -*- coding: utf-8 -*-
"""新手引导的后端：把「能不能跑起来」拆成 6 步，每步都能单独检测、单独修。

设计原则
--------
1. 检测只读，不动用户文件；下载 / 启动这类动作必须由用户点按钮触发。
2. 每一步都回答三个问题：现在什么状态、为什么、下一步该点哪儿。
3. 能用就通过（比如只有极简档也允许继续，只提示出不了动画），不拿完美主义吓人。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

import pipeline as pl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMFY = os.path.join(ROOT, "ComfyUI")
MANIFEST = os.path.join(ROOT, "models", "manifest.json")
OUT_DIR = os.path.join(HERE, "输出")

TEST_PROMPT = ("像素风游戏角色，正面站立姿势，全身，轮廓清晰，白色纯色背景，"
               "配色鲜明，细节简洁，适合做成 sprite 动画")

_lock = threading.Lock()
_gpu_cache = {"t": 0.0, "v": None}
_checks_cache = {"t": 0.0, "v": None, "force": 0}
_extra_out = {"fn": None}          # webui 注入：当前挂机输出目录

_state = {
    "comfy": {"busy": False, "log": [], "error": "", "doneAt": 0, "started": 0},
    "dl": {"busy": False, "tier": "", "line": "", "pct": 0, "log": [],
           "error": "", "proc": None, "doneAt": 0, "ok": False},
    "test": {"state": "todo", "busy": False, "log": [], "error": "",
             "png": None, "doneAt": 0, "secs": 0},
}

COMFY_LOG = os.path.join(HERE, "_comfyui.log")


# ---------------------------------------------------------------- 基础设施
def set_out_dir_getter(fn):
    """让 webui 把「当前挂机输出目录」的取值函数注册进来。"""
    _extra_out["fn"] = fn


def _log(bucket, msg, cap=160):
    box = _state[bucket]["log"]
    box.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
    if len(box) > cap:
        del box[:cap // 3]


def _gpu():
    now = time.time()
    with _lock:
        if _gpu_cache["v"] is not None and now - _gpu_cache["t"] < 15:
            return _gpu_cache["v"]
    try:
        import check_env
        info = check_env.gpu_info()
    except Exception:
        info = None
    with _lock:
        _gpu_cache["v"] = info
        _gpu_cache["t"] = now
    return info


def _manifest():
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _tier_files(tier):
    man = _manifest()
    conf = (man.get("tiers") or {}).get(tier) or {}
    targets = conf.get("targets") or []
    labels = {x.get("target"): x for x in man.get("files", [])}
    have, miss = [], []
    for t in targets:
        item = labels.get(t) or {}
        rec = {"target": t, "label": item.get("label", t),
               "gb": round(int(item.get("size") or 0) / 1e9, 1)}
        (have if pl.has_model(t) else miss).append(rec)
    return {"label": conf.get("label", tier), "have": have, "miss": miss,
            "gb": round(int(conf.get("bytes") or 0) / 1e9, 1)}


def _probe_writable(path):
    """在目录里写一个自己的临时文件再删掉，确认真的能落盘。"""
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return False, "建不了这个文件夹：%s" % e
    probe = os.path.join(path, ".pixel_write_test")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True, ""
    except Exception as e:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except Exception:
            pass
        return False, str(e)


def _step(sid, title, state, headline, detail=None, fix=None, actions=None,
          optional=False):
    return {"id": sid, "title": title, "state": state, "headline": headline,
            "detail": detail or [], "fix": fix or [], "actions": actions or [],
            "optional": optional}


# ---------------------------------------------------------------- 六步检测
def _tail(path, n=1000):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n * 4))
            raw = f.read().decode("utf-8", "ignore")
        return raw.strip()[-n:]
    except Exception:
        return ""


def _step_files():
    main_py = os.path.join(COMFY, "main.py")
    nodes = os.path.join(COMFY, "custom_nodes", "pixel_memory")
    node_ok = os.path.isdir(nodes) and bool(
        [x for x in os.listdir(nodes) if x.endswith(".py")]) if os.path.isdir(nodes) else False

    detail = [
        {"k": "程序目录", "v": ROOT},
        {"k": "ComfyUI", "v": "已找到" if os.path.isfile(main_py) else "缺失"},
        {"k": "内存暂存节点", "v": "已找到" if node_ok else "缺失"},
    ]
    if not os.path.isfile(main_py):
        return _step("files", "程序文件", "fail",
                     "ComfyUI 主体不在这台电脑上，安装包可能没解压完整。",
                     detail,
                     ["确认解压时没有漏掉 ComfyUI 文件夹（应该和 MyPixelTool 在同一层）",
                      "如果确实没有，重新解压完整的整合包，或双击「一键安装.bat」重新安装",
                      "解压路径不要放在中文名很长的深层目录里，例如 C:\\像素工具\\ 就可以"],
                     [{"id": "open_root", "label": "打开程序目录", "kind": "ghost"}])
    if not node_ok:
        return _step("files", "程序文件", "fail",
                     "少了自带的「内存暂存」节点，生成结果没法不回写硬盘。",
                     detail,
                     ["看看 ComfyUI\\custom_nodes\\pixel_memory 这个文件夹在不在",
                      "不在的话，把分享包里的 custom_nodes 整个覆盖过去",
                      "覆盖后必须关掉 ComfyUI 的黑窗口再重新启动"],
                     [{"id": "open_nodes", "label": "打开节点目录", "kind": "ghost"}])
    return _step("files", "程序文件", "ok",
                 "程序文件都在，这一步没问题。", detail)


def _step_gpu():
    gpu = _gpu()
    detail = []
    if gpu:
        detail.append({"k": "显卡", "v": gpu.get("name", "未知")})
        detail.append({"k": "显存", "v": ("%.1f GB" % gpu["vram"]) if gpu.get("vram") else "读取不到"})
        if gpu.get("driver"):
            detail.append({"k": "驱动", "v": gpu["driver"]})
    else:
        detail.append({"k": "显卡", "v": "没有检测到 NVIDIA 显卡"})

    if gpu and gpu.get("noSmi"):
        return _step("gpu", "显卡与驱动", "fail",
                     "系统里有 NVIDIA 显卡，但驱动没装好 —— 程序现在把它当没有显卡处理。",
                     detail,
                     ["去 NVIDIA 官网（或 NVIDIA App）下载最新的 GeForce 驱动",
                      "安装时选「精简安装」就行，装完一定要重启电脑",
                      "重启后再点下面的「重新检测」，这里会变绿",
                      "如果只是想在别的机器上跑，可以点「我用局域网方案」跳过"],
                     [{"id": "recheck", "label": "重新检测", "kind": "primary"},
                      {"id": "lan_help", "label": "我用局域网方案", "kind": "ghost"}])
    if not gpu:
        return _step("gpu", "显卡与驱动", "warn",
                     "没检测到 NVIDIA 显卡：本机出图会很慢、出动画基本不可用，但可以连别的电脑用。",
                     detail,
                     ["这台机器仍然可以用：在有 N 卡的电脑上双击「启动服务端(局域网).bat」，",
                      "然后在浏览器打开它显示的 http://192.168.x.x:8899 地址，功能完全一样",
                      "如果这台机器其实有 N 卡，多半是驱动没装 —— 装驱动后重启再看"],
                     [{"id": "recheck", "label": "重新检测", "kind": "ghost"},
                      {"id": "lan_help", "label": "怎么连别的电脑", "kind": "ghost"}],
                     optional=True)
    vram = gpu.get("vram") or 0
    if vram and vram < 6:
        return _step("gpu", "显卡与驱动", "warn",
                     "显卡能认出来，但显存只有 %.0fGB：能出图，分辨率会自动降一点。" % vram,
                     detail,
                     ["出图时分辨率会自动压到 896 左右，速度会慢一些，属正常",
                      "出动画（视频模型）在这张卡上会很勉强，建议只用像素图模式",
                      "跑的时候别同时开太多网页和游戏，显存不够会直接失败"],
                     [{"id": "recheck", "label": "重新检测", "kind": "ghost"}])
    return _step("gpu", "显卡与驱动", "ok",
                 "显卡和驱动都正常，%.0fGB 显存，可以开始下一步。" % (vram or 0), detail,
                 actions=[{"id": "recheck", "label": "重新检测", "kind": "ghost"}])


def _step_models():
    prof = pl.model_profile()
    lite = _tier_files("lite")
    full = _tier_files("full")
    detail = [
        {"k": "出图模型", "v": "已装" if prof.get("canImage") else "缺失"},
        {"k": "动画模型", "v": "已装" if prof.get("canVideo") else "未安装"},
        {"k": "去背景模型", "v": "已装" if prof.get("hasRMBG") else "未安装"},
        {"k": "当前档位", "v": "全量档（出图 + 动画）" if prof.get("canVideo") else "极简档（只出图片）"},
    ]
    if not lite["miss"]:
        detail.append({"k": "极简档", "v": "完整（约 15GB）"})
    if not full["miss"]:
        detail.append({"k": "全量档", "v": "完整（约 69GB）"})

    dl = _state["dl"]
    acts = []
    if not prof.get("canImage"):
        acts.append({"id": "download_lite", "label": "下载极简档（15GB）", "kind": "primary"})
        acts.append({"id": "download_full", "label": "下载全量档（69GB）", "kind": "ghost"})
        head = "还没有可用的模型，先下载一份才能出图。"
        state = "fail"
        fix = [
            "点上面的按钮，程序会在后台自动下载并校验（支持断点续传，断了重跑会接着下）",
            "极简档约 15GB，只出静态像素图；全量档约 69GB，能出动画",
            "下载时间取决于网速，可以先去忙别的，进度会显示在这里",
            "如果下载卡住不动，换个网络或关掉代理再点一次",
        ]
    elif not prof.get("canVideo"):
        state = "warn"
        head = "已经能出像素图了；还没装动画模型，所以暂时出不了动作序列。"
        acts.append({"id": "download_full", "label": "补下动画模型（54GB）", "kind": "ghost"})
        fix = [
            "只想做静态像素图的话，这一步不用管，直接下一步",
            "想出动作/动画，就点「补下动画模型」，下完自动接进 ComfyUI",
            "下载完要重启一次 ComfyUI（向导里会带你做）",
        ]
    else:
        state = "ok"
        head = "模型齐全，出图和出动画都没问题。"
        fix = []

    if state == "fail" and full["miss"] and lite["miss"]:
        miss = [x for x in lite["miss"]][:4]
        for m in miss:
            detail.append({"k": "缺 " + m["label"][:14], "v": "%.1f GB" % m["gb"], "bad": True})

    if dl["busy"]:
        state_now = "busy"
        head = "正在下载模型（%s 档）：%s" % (dl["tier"], dl["line"] or "准备中…")
        acts = [{"id": "stop_download", "label": "停止下载", "kind": "ghost"}]
        return _step("models", "模型文件", state_now, head, detail, [], acts)

    if dl.get("error"):
        fix = [("上次下载中断：%s" % dl["error"])] + fix

    return _step("models", "模型文件", state, head, detail, fix, acts)


def _step_comfy():
    alive = pl.server_alive()
    nodes = pl.mem_nodes_ready() if alive else False
    st = _state["comfy"]
    detail = [
        {"k": "ComfyUI 服务", "v": "运行中（端口 %d）" % pl.PORT if alive else "没在运行"},
        {"k": "内存暂存节点", "v": "已加载" if nodes else ("未加载" if alive else "—")},
    ]
    if st["busy"]:
        last = st["log"][-1].split("  ", 1)[-1] if st["log"] else "正在启动…"
        waited = int(time.time() - (st.get("started") or time.time()))
        return _step("comfy", "ComfyUI 服务", "busy",
                     "正在启动 ComfyUI（已等 %d 秒）：%s" % (waited, last), detail)
    if alive and nodes:
        return _step("comfy", "ComfyUI 服务", "ok",
                     "服务已经跑起来了，可以出图。", detail,
                     actions=[{"id": "recheck", "label": "重新检测", "kind": "ghost"}])
    if alive and not nodes:
        return _step("comfy", "ComfyUI 服务", "fail",
                     "ComfyUI 在跑，但它是旧版本（没加载到内存暂存节点）。",
                     detail,
                     ["找到 ComfyUI 的黑色命令行窗口，直接关掉它",
                      "回到这一步，点「启动 ComfyUI」重新开一个",
                      "如果关不掉，用任务管理器结束 python.exe 再试"],
                     [{"id": "start_comfy", "label": "启动 ComfyUI", "kind": "primary"},
                      {"id": "recheck", "label": "重新检测", "kind": "ghost"}])
    return _step("comfy", "ComfyUI 服务", "fail",
                 "ComfyUI 没在运行 —— 它负责真正的画图，必须先起来。",
                 detail,
                 ["点下面的「启动 ComfyUI」，第一次启动要 30~60 秒（要加载模型）",
                  "这期间电脑会风扇变响、占用显存，属正常",
                  "如果启动失败，看下面的日志：端口被占用就重启电脑；缺依赖就双击「一键安装.bat」",
                  "启动过程中不要重复点按钮"],
                 [{"id": "start_comfy", "label": "启动 ComfyUI", "kind": "primary"},
                  {"id": "recheck", "label": "重新检测", "kind": "ghost"}])


def _step_test():
    t = _state["test"]
    detail = []
    if t.get("secs"):
        detail.append({"k": "上次耗时", "v": "%.0f 秒" % t["secs"]})
    if t["busy"]:
        last = t["log"][-1].split("  ", 1)[-1] if t["log"] else "准备中…"
        return _step("test", "试画一张", "busy", "正在试画：%s" % last, detail,
                     optional=True)
    if t["state"] == "ok":
        return _step("test", "试画一张", "ok",
                     "试画成功！说明整条链路是通的，可以开始正式创作了。",
                     detail + [{"k": "结果", "v": "下面这张就是刚才画的"}],
                     actions=[{"id": "test_again", "label": "再试一张", "kind": "ghost"}],
                     optional=True)
    if t["state"] == "fail":
        return _step("test", "试画一张", "fail",
                     "试画失败了。看下面的原因，改完再点一次。", detail,
                     _test_fix(t.get("error", "")),
                     [{"id": "test_image", "label": "再试一次", "kind": "primary"},
                      {"id": "recheck", "label": "重新检测环境", "kind": "ghost"}],
                     optional=True)
    return _step("test", "试画一张", "todo",
                 "前面几步都通了，建议先花一分钟试画一张，确认真的能出图。", detail,
                 ["点「试画一张」，会用最小分辨率画一张像素角色（约 30~90 秒）",
                  "成功后会显示图片 —— 看到图就说明全部搞定了",
                  "如果失败，这里会直接告诉你卡在哪：显卡爆显存、模型没加载、服务断了……"],
                 [{"id": "test_image", "label": "试画一张", "kind": "primary"}],
                 optional=True)


def _step_output():
    targets = [("默认输出目录", OUT_DIR)]
    fn = _extra_out["fn"]
    if fn:
        try:
            d = fn() or ""
        except Exception:
            d = ""
        if d and os.path.abspath(d) != os.path.abspath(OUT_DIR):
            targets.append(("挂机输出目录", d))

    detail, bad = [], []
    for name, path in targets:
        ok, err = _probe_writable(path)
        detail.append({"k": name, "v": path})
        detail.append({"k": "可写入", "v": "是" if ok else ("否：" + err), "bad": not ok})
        if not ok:
            bad.append((name, path))

    if bad:
        return _step("output", "保存位置", "fail",
                     "有些输出目录写不进去，生成完会保存失败。", detail,
                     ["把输出目录换到你有权限的位置，例如 D:\\像素输出 或「文档」下面",
                      "不要选 C:\\Program Files、C:\\Windows 这类系统目录",
                      "也不要用正在被网盘/杀毒软件同步锁定的文件夹",
                      "在主界面第 5 步「挂机生产」里可以重新选择输出目录"],
                     [{"id": "open_out", "label": "打开输出目录", "kind": "ghost"}])
    return _step("output", "保存位置", "ok",
                 "输出目录可写，生成的东西能正常存下来。", detail,
                 actions=[{"id": "open_out", "label": "打开输出目录", "kind": "ghost"}])


def checks(force=False):
    now = time.time()
    if not force and _checks_cache["v"] is not None and now - _checks_cache["t"] < 6:
        return _checks_cache["v"]
    steps = [_step_files(), _step_gpu(), _step_models(), _step_comfy(),
             _step_test(), _step_output()]
    blocking = [s for s in steps if not s["optional"]]
    ready = all(s["state"] in ("ok", "warn") for s in blocking)
    first_bad = None
    for s in steps:
        if s["state"] in ("fail", "todo") and not (s["optional"] and s["state"] == "todo"):
            first_bad = s["id"]
            break
    done = len([s for s in steps if s["state"] in ("ok", "warn")])
    res = {
        "steps": steps, "ready": ready, "firstBad": first_bad,
        "done": done, "total": len(steps),
        "profile": pl.model_profile(),
        "comfy": {"busy": _state["comfy"]["busy"], "log": _state["comfy"]["log"][-40:]},
        "download": {"busy": _state["dl"]["busy"], "tier": _state["dl"]["tier"],
                     "line": _state["dl"]["line"], "pct": _state["dl"]["pct"],
                     "log": _state["dl"]["log"][-40:], "error": _state["dl"]["error"]},
        "test": {"state": _state["test"]["state"], "busy": _state["test"]["busy"],
                 "log": _state["test"]["log"][-40:], "hasPng": bool(_state["test"]["png"])},
        "root": ROOT,
    }
    with _lock:
        _checks_cache["v"] = res
        _checks_cache["t"] = now
    return res


# ---------------------------------------------------------------- 动作：启动 ComfyUI
def start_comfy():
    with _lock:
        if _state["comfy"]["busy"]:
            return {"ok": True, "busy": True}
        _state["comfy"].update({"busy": True, "log": [], "error": "",
                                "started": time.time()})
    t = threading.Thread(target=_comfy_worker, daemon=True)
    t.start()
    return {"ok": True, "busy": True}


def _comfy_worker():
    try:
        def emit(stage, msg, percent=0):
            _log("comfy", msg)

        if pl.server_alive() and pl.mem_nodes_ready():
            _log("comfy", "ComfyUI 已经在运行，无需重复启动")
        else:
            if not os.path.isfile(os.path.join(COMFY, "main.py")):
                raise RuntimeError("找不到 ComfyUI\\main.py，先回到第 1 步检查程序文件")
            _log("comfy", "正在启动 ComfyUI（首次约 30~60 秒，请稍等）…")
            cmd = [pl.ENV_PY, "-s", "main.py", "--port", str(pl.PORT), "--disable-auto-launch"]
            # 注意：不能接 PIPE 又不读 —— ComfyUI 启动时会吐几十 KB 日志，
            # 管道缓冲一满进程就卡死，表现为「一直启动中」。直接写文件，出错再读尾巴。
            logf = open(COMFY_LOG, "wb")
            try:
                proc = subprocess.Popen(cmd, cwd=COMFY, stdout=logf, stderr=subprocess.STDOUT)
            finally:
                logf.close()
            t0 = time.time()
            ready = False
            while time.time() - t0 < 240:
                time.sleep(1.5)
                if proc.poll() is not None:
                    raise RuntimeError("启动后马上退出了。日志尾部：%s"
                                       % (_tail(COMFY_LOG) or "（无输出）"))
                if pl.server_alive():
                    ready = True
                    break
            if not ready:
                raise RuntimeError("等了 4 分钟还没启动起来。可能是端口 8188 被占用，或模型太大加载慢，"
                                   "关掉多余程序后点「重新检测」，或重启电脑再试")
            _log("comfy", "服务已就绪，等待节点加载…")
            for _ in range(60):
                if pl.mem_nodes_ready():
                    break
                time.sleep(2)
        _log("comfy", "ComfyUI 就绪 ✓")
        _state["comfy"]["doneAt"] = time.time()
    except Exception as e:
        traceback.print_exc()
        _state["comfy"]["error"] = str(e)
        _log("comfy", "启动失败：" + str(e))
    finally:
        with _lock:
            _state["comfy"]["busy"] = False
            _checks_cache["t"] = 0


# ---------------------------------------------------------------- 动作：下载模型
def start_download(tier):
    tier = "full" if str(tier).lower() == "full" else "lite"
    with _lock:
        if _state["dl"]["busy"]:
            return {"ok": False, "error": "已经有一个下载任务在跑了"}
        _state["dl"].update({"busy": True, "tier": tier, "line": "", "pct": 0,
                             "log": [], "error": "", "ok": False, "doneAt": 0})
    threading.Thread(target=_download_worker, args=(tier,), daemon=True).start()
    return {"ok": True, "tier": tier}


def stop_download():
    with _lock:
        proc = _state["dl"].get("proc")
    if proc and proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
            _log("dl", "已停止下载（已下载的部分会保留，下次接着下）")
            _state["dl"]["error"] = "已手动停止"
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True}


def _pump(proc, on_line):
    """逐字符读 stdout，\r 和 \n 都当换行（下载脚本用 \r 刷进度条）。"""
    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch in ("\r", "\n"):
            if buf.strip():
                on_line(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        on_line(buf.strip())


def _download_worker(tier):
    dest = os.path.join(ROOT, "models")
    try:
        if not os.path.isfile(os.path.join(ROOT, "download_models.py")):
            raise RuntimeError("找不到 download_models.py，分享包可能不完整")
        _log("dl", "开始下载 %s 档到 %s" % (tier, dest))
        cmd = [pl.ENV_PY, "-u", os.path.join(ROOT, "download_models.py"),
               "--dest", dest, "--tier", tier]
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        with _lock:
            _state["dl"]["proc"] = proc

        def on_line(line):
            _state["dl"]["line"] = line
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            if m:
                try:
                    _state["dl"]["pct"] = float(m.group(1))
                except Exception:
                    pass
            else:
                _log("dl", line)

        _pump(proc, on_line)
        code = proc.wait()
        if code != 0:
            raise RuntimeError("下载脚本退出码 %s（网络中断或磁盘满了）" % code)

        _log("dl", "下载完成，正在接进 ComfyUI…")
        link = subprocess.run([pl.ENV_PY, "-s", os.path.join(HERE, "setup_models.py"), "--yes"],
                              cwd=HERE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        for ln in (link.stdout or "").splitlines():
            if ln.strip():
                _log("dl", ln.strip())
        _state["dl"]["ok"] = True
        _log("dl", "全部完成 ✓")
    except Exception as e:
        traceback.print_exc()
        _state["dl"]["error"] = str(e)
        _log("dl", "下载失败：" + str(e))
    finally:
        with _lock:
            _state["dl"]["busy"] = False
            _state["dl"]["proc"] = None
            _state["dl"]["doneAt"] = time.time()
            _checks_cache["t"] = 0
            _gpu_cache["v"] = None


# ---------------------------------------------------------------- 动作：试画一张
def start_test():
    with _lock:
        if _state["test"]["busy"]:
            return {"ok": False, "error": "正在试画，稍等"}
        _state["test"].update({"busy": True, "state": "run", "log": [],
                               "error": "", "png": None, "secs": 0})
    threading.Thread(target=_test_worker, daemon=True).start()
    return {"ok": True}


def _test_fix(err):
    s = (err or "").lower()
    if "显存" in s or "memory" in s or "oom" in s or "allocation" in s:
        return ["显存不够：关掉浏览器其他标签、游戏、剪辑软件，再点「再试一次」",
                "还不行就把分辨率调小一点（主界面里选 896），或者重启电脑释放显存",
                "8GB 显卡跑 1328 分辨率会爆，试画用的是 512，正常应该没问题"]
    if "模型" in s or "model" in s or "not found" in s:
        return ["回到第 3 步「模型文件」，确认出图模型是「已装」",
                "如果刚才补下过模型，需要重启 ComfyUI 才能认出来（第 4 步有按钮）",
                "文件可能没下完整：重跑下载会校验大小，不完整的会自动补"]
    if "连接" in s or "refused" in s or "timeout" in s or "超时" in s:
        return ["ComfyUI 服务掉了：回第 4 步点「启动 ComfyUI」，等它变绿再来",
                "如果它一直在跑却连不上，可能是端口被别的程序占了，重启电脑最省事"]
    if "节点" in s or "node" in s:
        return ["这是自定义节点没加载：第 1 步能通过的话，多半是 ComfyUI 需要重启",
                "重启仍然报错就重新覆盖 custom_nodes\\pixel_memory 目录"]
    return ["先点「重新检测环境」，确认前三步都是绿色",
            "如果反复失败，把这里的错误信息连同 ComfyUI 黑窗口的日志一起发出来"]


def _test_worker():
    t0 = time.time()
    try:
        def emit(stage, msg, percent=0):
            _log("test", msg)

        emit("准备", "检查 ComfyUI 服务…")
        pl.ensure_server(emit)
        w, h, _note = pl.cap_size_for_vram(512, 512)
        emit("准备", "开始试画（%dx%d，4 步）…" % (w, h))
        key = pl.new_key("selftest_")
        graph = pl.build_t2i(TEST_PROMPT, w, h, 4, 20261107, key)
        pl.queue_workflow(graph, "自检出图", emit, 5, 85, None, 900)
        png = pl.mem_get(key, 0)
        try:
            pl.mem_drop(key)
        except Exception:
            pass
        if not png:
            raise RuntimeError("图生成完了但取不回来（内存暂存节点可能没生效）")
        with _lock:
            _state["test"]["png"] = png
            _state["test"]["state"] = "ok"
            _state["test"]["secs"] = time.time() - t0
        emit("完成", "试画成功，用时 %.0f 秒" % (time.time() - t0))
    except Exception as e:
        traceback.print_exc()
        with _lock:
            _state["test"]["state"] = "fail"
            _state["test"]["error"] = str(e)
        _log("test", "失败：" + str(e))
    finally:
        with _lock:
            _state["test"]["busy"] = False
            _state["test"]["doneAt"] = time.time()
            _checks_cache["t"] = 0


def test_png():
    with _lock:
        return _state["test"].get("png")


def open_path(which):
    targets = {
        "root": ROOT,
        "nodes": os.path.join(COMFY, "custom_nodes", "pixel_memory"),
        "out": OUT_DIR,
    }
    p = targets.get(which) or ROOT
    try:
        if os.path.isdir(p):
            os.startfile(p)          # noqa: S606  Windows 专用
            return {"ok": True, "path": p}
        return {"ok": False, "error": "目录不存在：%s" % p}
    except Exception as e:
        return {"ok": False, "error": str(e)}
