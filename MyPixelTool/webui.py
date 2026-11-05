# -*- coding: utf-8 -*-
"""MyPixelTool 网页界面后端（标准库实现，端口 8899）

设计要点：
- 候选角色图 / 生成的动作帧全部只存在内存里；
- 只有用户点「接受」时才写盘，反复试错不磨损硬盘。
"""
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pipeline as pl

WEB_DIR = os.path.join(pl.HERE, "web")
PORT = 8899

MAX_CANDS = 24          # 候选图最多保留数量
MAX_PENDING = 6         # 待接受动作最多保留数量

ACT_LIB = os.path.join(pl.OUT_ROOT, "_动作库.json")   # 自定义动作名（只存名字，不存图片）

_lock = threading.Lock()
_job = {
    "status": "idle",      # idle | running | done | error | cancelled
    "kind": "",            # candidates | actions | restore
    "stage": "",
    "message": "",
    "percent": 0,
    "logs": [],
    "name": "",
    "error": "",
    "started": 0,
    "result": None,
}
_cancel_flag = {"v": False}
_thread = None

# 内存池
_cands = {}             # cid -> {"key","png","seed","ts"}
_cand_order = []
_selected_cid = None
_char = {"key": None, "png": None, "label": "", "asset": ""}   # 当前角色图（内存）
_pending = {}           # pid -> {name, action, frames, pixel, duration, video, char_png, ts}
_pending_order = []


def _reset_job(name, kind):
    with _lock:
        _job.update({"status": "running", "kind": kind, "stage": "准备", "message": "开始…",
                     "percent": 0, "logs": [], "name": name, "error": "", "started": time.time(),
                     "result": None})
        _cancel_flag["v"] = False


def _emit(stage, msg, percent=0):
    with _lock:
        _job["stage"] = stage
        _job["message"] = msg
        if _job["status"] == "running":
            _job["percent"] = max(_job["percent"], int(percent))
        _job["logs"].append({"t": time.strftime("%H:%M:%S"), "stage": stage, "msg": msg})
        if len(_job["logs"]) > 400:
            del _job["logs"][:100]


def _ensure_char_key():
    """角色图在内存里的 key 可能因 ComfyUI 重启失效，这里做自愈重传"""
    key, png = _char.get("key"), _char.get("png")
    if not png:
        return None
    # 先确保 ComfyUI 在跑：它被关掉过的话，直接重传会「连接被拒绝」
    pl.ensure_server(_emit)
    if key and pl.mem_meta(key).get("count"):
        return key
    key = pl.mem_put(png, pl.new_key("char_"))
    _char["key"] = key
    return key


def _run_candidates(params, count):
    try:
        items = pl.generate_candidates(params, count, _emit, lambda: _cancel_flag["v"])
        ids = []
        with _lock:
            for it in items:
                cid = pl.new_key("c")
                _cands[cid] = {"key": it["key"], "png": it["png"], "seed": it["seed"], "ts": time.time()}
                _cand_order.append(cid)
                ids.append(cid)
            while len(_cand_order) > MAX_CANDS:
                old = _cand_order.pop(0)
                _cands.pop(old, None)
            _job.update({"status": "done", "percent": 100, "stage": "完成",
                         "message": f"已生成 {len(ids)} 张候选图（未落盘）", "result": {"cids": ids}})
    except Exception as e:
        _fail(e)


def _run_actions(params):
    name = params["name"]
    try:
        char_key = _ensure_char_key()
        if not char_key:
            raise RuntimeError("还没有角色图：请先生成候选图、选一张或上传图片")
        results = pl.render_actions(params, char_key, params.get("actions") or ["站立"],
                                    _emit, lambda: _cancel_flag["v"])
        ids = []
        with _lock:
            for r in results:
                pid = pl.new_key("p")
                _pending[pid] = {
                    "name": name, "action": r["action"], "frames": r["frames"],
                    "pixel": r["pixel"], "duration": r["duration"], "video": r.get("video"),
                    "char_png": _char.get("png"), "ts": time.time(),
                }
                _pending_order.append(pid)
                ids.append(pid)
            while len(_pending_order) > MAX_PENDING:
                old = _pending_order.pop(0)
                _pending.pop(old, None)
            _job.update({"status": "done", "percent": 100, "stage": "完成",
                         "message": "生成完成，等待你确认保存", "result": {"pids": ids}})
    except Exception as e:
        _fail(e)


def _fail(e):
    cancelled = _cancel_flag["v"] or "已取消" in str(e)
    with _lock:
        _job.update({"status": "cancelled" if cancelled else "error",
                     "error": "" if cancelled else str(e),
                     "message": "已取消" if cancelled else f"出错：{e}"})
    if not cancelled:
        traceback.print_exc()


# ---------------- 动作库（自定义动作名持久化） ----------------

def load_action_lib():
    try:
        with open(ACT_LIB, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("actions", [])
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def save_action_lib(items):
    os.makedirs(pl.OUT_ROOT, exist_ok=True)
    clean, seen = [], set()
    for x in items or []:
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            clean.append(x)
    with open(ACT_LIB, "w", encoding="utf-8") as f:
        json.dump({"actions": clean}, f, ensure_ascii=False, indent=2)
    return clean


def use_asset_char(name):
    """把已保存资产的角色图重新读进内存，方便继续给它生成新动作。"""
    global _selected_cid
    p = os.path.join(pl.OUT_ROOT, pl.safe_name(name), "角色图.png")
    if not name or not safe_path(p) or not os.path.isfile(p):
        return False
    with open(p, "rb") as f:
        png = f.read()
    key = None
    try:
        key = pl.mem_put(png, pl.new_key("char_"))
    except Exception as e:
        # ComfyUI 没开就先不传，点「生成动作」时 _ensure_char_key 会自动重传
        print("角色图暂存内存池失败（生成时会自动重试）：", e)
    with _lock:
        _selected_cid = None
        _char.update({"key": key, "png": png, "label": name, "asset": name})
    return True


# ---------------- 挂机模式（队列 + 生成即落盘） ----------------

HANG_SETTINGS = os.path.join(pl.OUT_ROOT, "_挂机设置.json")

_hang = {
    "queue": [],          # [{"name","prompt"}] 用户维护的队列
    "outDir": "",         # 输出根目录（空 = 默认 MyPixelTool\输出）
    "actions": [],        # 上次执行的动作序列
    "status": "idle",     # idle | running | done | cancelled | error
    "idx": 0,             # 当前第几个资产（0 起）
    "total": 0,           # 队列长度
    "curName": "",
    "curAction": "",
    "stage": "",
    "message": "",
    "percent": 0,
    "logs": [],
    "done": 0,            # 已完成动作数
    "plan": 0,            # 计划动作总数 = n × m
    "fail": [],           # [{"name","error"}]
    "started": 0,
    "finished": 0,
}
_hang_cancel = {"v": False}
# 进度折算：每个资产 = 1 个「出图」单元 + m 个「动作」单元，保证百分比全程单调
_hang_ctx = {"done": 0.0, "total": 1.0}


def load_hang_settings():
    try:
        with open(HANG_SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    q = []
    for x in d.get("queue") or []:
        if isinstance(x, dict):
            n, p = str(x.get("name", "")).strip(), str(x.get("prompt", "")).strip()
            if n or p:
                q.append({"name": n, "prompt": p})
    with _lock:
        _hang["queue"] = q
        _hang["outDir"] = str(d.get("outDir") or "")
        _hang["actions"] = [str(a) for a in (d.get("actions") or []) if str(a).strip()]


def save_hang_settings():
    with _lock:
        data = {"queue": [dict(x) for x in _hang["queue"]],
                "outDir": _hang["outDir"], "actions": list(_hang["actions"])}
    try:
        os.makedirs(pl.OUT_ROOT, exist_ok=True)
        with open(HANG_SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存挂机设置失败：", e)


def hang_out_dir():
    with _lock:
        d = (_hang.get("outDir") or "").strip()
    return os.path.abspath(d) if d else pl.OUT_ROOT


def _hang_log(msg, stage="·"):
    with _lock:
        _hang["logs"].append({"t": time.strftime("%H:%M:%S"), "stage": stage, "msg": msg})
        if len(_hang["logs"]) > 600:
            del _hang["logs"][:200]


def _hang_emit(stage, msg, percent=0):
    """把当前单元内部的 0~100 进度折算成整队列的百分比"""
    with _lock:
        total = max(1.0, _hang_ctx["total"])
        sub = max(0.0, min(1.0, (int(percent) if percent else 0) / 100.0))
        overall = (_hang_ctx["done"] + sub) / total * 100
        _hang["stage"] = stage
        _hang["message"] = msg
        _hang["percent"] = min(99, max(_hang["percent"], int(overall)))
        _hang["logs"].append({"t": time.strftime("%H:%M:%S"), "stage": stage, "msg": msg})
        if len(_hang["logs"]) > 600:
            del _hang["logs"][:200]


def _hang_runner(items, actions, params, out_dir):
    """串行跑完整个队列：每项出 1 张角色图 → 依次做全部动作 → 每个动作立即落盘"""
    total = len(items)
    m = max(1, len(actions))
    try:
        pl.ensure_server(_hang_emit)
        for i, it in enumerate(items):
            if _hang_cancel["v"]:
                raise RuntimeError("已取消")
            name = (it.get("name") or "").strip() or f"资产{i + 1}"
            prompt = (it.get("prompt") or "").strip()
            with _lock:
                _hang.update({"idx": i, "curName": name, "curAction": "",
                              "stage": "文生图", "message": f"[{i + 1}/{total}] {name} · 生成角色图"})
                _hang_ctx.update(done=float(i * (1 + m)), total=float(total * (1 + m)))
            try:
                if not prompt:
                    raise RuntimeError("提示词为空，已跳过")
                cands = pl.generate_candidates(
                    {"prompt": prompt, "size": params.get("size") or "1328x1328",
                     "steps": params.get("steps", 8)}, 1, _hang_emit, lambda: _hang_cancel["v"])
                png = cands[0]["png"]                       # 无脑取第一张
                key = pl.mem_put(png, pl.new_key("char_"))
                pl.save_char_image(name, png, root=out_dir)
                man = pl.load_manifest(name, out_dir)
                man["prompt"] = prompt
                pl.save_manifest(name, man, out_dir)
                try:
                    pl.mem_drop(cands[0]["key"])
                except Exception:
                    pass
                _hang_log(f"✓ {name} · 角色图已存盘", "角色")

                for j, act in enumerate(actions):
                    if _hang_cancel["v"]:
                        raise RuntimeError("已取消")
                    with _lock:
                        _hang.update({"curAction": act, "stage": "图生视频",
                                      "message": f"[{i + 1}/{total}] {name} · {act}"})
                        _hang_ctx["done"] = float(i * (1 + m) + 1 + j)
                    res = pl.render_actions({**params, "name": name}, key, [act],
                                            _hang_emit, lambda: _hang_cancel["v"])
                    r = res[0]
                    pl.save_action(name, act, r["frames"], r["pixel"], r["duration"],
                                   r.get("video"), root=out_dir)
                    with _lock:
                        _hang["done"] += 1
                    _hang_log(f"✓ {name} · {act} 已存盘（{len(r['frames'])} 帧）", "完成")
            except RuntimeError as e:
                if "已取消" in str(e):
                    raise
                with _lock:
                    _hang["fail"].append({"name": name, "error": str(e)})
                _hang_log(f"✗ {name} 失败：{e}（跳过，继续下一个）", "错误")
            except Exception as e:
                traceback.print_exc()
                with _lock:
                    _hang["fail"].append({"name": name, "error": str(e)})
                _hang_log(f"✗ {name} 失败：{e}（跳过，继续下一个）", "错误")

        with _lock:
            fails = len(_hang["fail"])
            _hang.update({"status": "done", "percent": 100, "stage": "完成",
                          "message": f"队列跑完：成功 {_hang['done']}/{_hang['plan']} 个动作"
                                     + (f"，{fails} 个资产失败" if fails else ""),
                          "curAction": ""})
    except Exception as e:
        cancelled = _hang_cancel["v"] or "已取消" in str(e)
        with _lock:
            _hang.update({"status": "cancelled" if cancelled else "error",
                          "stage": "结束", "curAction": "",
                          "message": "已取消" if cancelled else f"出错：{e}"})
        if not cancelled:
            traceback.print_exc()
    finally:
        with _lock:
            _hang["finished"] = time.time()


def pick_folder(initial=""):
    """调 Windows 原生「选择文件夹」对话框。

    便携环境包里的 Python 没有 Tcl/Tk（tkinter 不可用），所以走 PowerShell +
    System.Windows.Forms.FolderBrowserDialog；结果写临时文件读取，绕开控制台编码问题。
    """
    import tempfile
    out_file = os.path.join(tempfile.gettempdir(),
                            f"pixel_pick_{os.getpid()}_{int(time.time() * 1000)}.txt")
    ini = (initial or "").replace("'", "''")
    ps = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='选择挂机输出目录（新文件夹可用右下角按钮创建）';"
        "$d.ShowNewFolderButton=$true;"
        f"if('{ini}' -and (Test-Path -LiteralPath '{ini}')){{$d.SelectedPath='{ini}'}};"
        "        if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
        f"[System.IO.File]::WriteAllText('{out_file}',$d.SelectedPath,"
        "[System.Text.Encoding]::UTF8)}"
    )
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW：不额外弹黑框
    try:
        subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                       capture_output=True, timeout=900, creationflags=flags)
    except Exception as e:
        print("选择目录失败：", e)
    finally:
        pass
    path = ""
    try:
        if os.path.isfile(out_file):
            with open(out_file, encoding="utf-8-sig") as f:
                path = f.read().strip()
            os.remove(out_file)
    except Exception:
        pass
    return path


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".__write_test__")
        with open(probe, "wb") as f:
            f.write(b"1")
        os.remove(probe)
        return True, ""
    except Exception as e:
        return False, str(e)


load_hang_settings()


# ---------------- 数据 ----------------

def list_assets():
    out = []
    if not os.path.isdir(pl.OUT_ROOT):
        return out
    for name in sorted(os.listdir(pl.OUT_ROOT)):
        d = os.path.join(pl.OUT_ROOT, name)
        if not os.path.isdir(d):
            continue
        man = pl.load_manifest(name)
        acts, seen = [], set()
        for a, info in man.get("actions", {}).items():
            fd = os.path.join(d, a, "像素帧")
            if os.path.isdir(fd):
                acts.append({"action": a, "frames": len([f for f in os.listdir(fd) if f.endswith(".png")])})
                seen.add(a)
        for a in sorted(os.listdir(d)):
            fd = os.path.join(d, a, "像素帧")
            if a not in seen and os.path.isdir(fd):
                acts.append({"action": a, "frames": len([f for f in os.listdir(fd) if f.endswith(".png")])})
        out.append({
            "name": name,
            "actions": acts,
            "hasChar": os.path.exists(os.path.join(d, "角色图.png")),
            "prompt": man.get("prompt", ""),
        })
    return out


def frame_list(name, action):
    fd = os.path.join(pl.OUT_ROOT, name, action, "像素帧")
    if not os.path.isdir(fd):
        return []
    files = sorted(f for f in os.listdir(fd) if f.endswith(".png"))
    man = pl.load_manifest(name)
    pixel = man.get("actions", {}).get(action, {}).get("pixel", 64)
    return [{"url": "/file?p=" + urllib.parse.quote(os.path.join(fd, f)), "pixel": pixel} for f in files]


def safe_path(p):
    p = os.path.abspath(p)
    return p.startswith(os.path.abspath(pl.ROOT) + os.sep)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------------- GET ----------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path, parts = u.path, u.path.strip("/").split("/")

        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if path == "/api/hang":
            with _lock:
                st = json.loads(json.dumps(_hang, ensure_ascii=False))
            st["logs"] = st["logs"][-250:]
            st["running"] = st["status"] == "running"
            st["outDirResolved"] = hang_out_dir()
            st["outDirDefault"] = pl.OUT_ROOT
            st["defaultExists"] = os.path.isdir(pl.OUT_ROOT)
            return self._send(200, st)

        if path == "/api/status":
            with _lock:
                job = dict(_job)
                job["logs"] = job["logs"][-200:]
            return self._send(200, {"comfy": pl.server_alive(), "memReady": pl.mem_nodes_ready(), "job": job})

        if path == "/api/assets":
            return self._send(200, {"assets": list_assets(), "actions": load_action_lib()})

        if path == "/api/actions":
            return self._send(200, {"actions": load_action_lib()})

        if path == "/api/frames":
            return self._send(200, {"frames": frame_list(q.get("name", [""])[0], q.get("action", [""])[0])})

        if path == "/api/manifest":
            return self._send(200, pl.load_manifest(q.get("name", [""])[0]))

        if path == "/api/char":
            name = q.get("name", [""])[0]
            p = os.path.join(pl.OUT_ROOT, name, "角色图.png")
            if not safe_path(p) or not os.path.isfile(p):
                return self._send(404, {"error": "no char image"})
            with open(p, "rb") as f:
                return self._send(200, f.read(), "image/png")

        if path == "/api/charimage":
            # 当前内存角色图（候选/上传/已保存资产都统一从这里预览）
            with _lock:
                png = _char.get("png")
            if not png:
                return self._send(404, {"error": "no char image"})
            return self._send(200, png, "image/png")

        # --- 内存池：候选图 ---
        if path == "/api/candidates":
            with _lock:
                items = [{"cid": cid, "seed": _cands[cid]["seed"]} for cid in _cand_order if cid in _cands]
                sel = _selected_cid
                char = {"label": _char.get("label", ""), "hasPng": bool(_char.get("png")),
                        "asset": _char.get("asset", "")}
            return self._send(200, {"items": items, "selected": sel, "char": char})

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "cand":
            cid = parts[2].rsplit(".", 1)[0]
            with _lock:
                item = _cands.get(cid)
            if not item:
                return self._send(404, {"error": "not found"})
            return self._send(200, item["png"], "image/png")

        # --- 内存池：待接受动作 ---
        if path == "/api/pending":
            with _lock:
                items = []
                for pid in _pending_order:
                    it = _pending.get(pid)
                    if it:
                        items.append({"pid": pid, "name": it["name"], "action": it["action"],
                                      "count": len(it["frames"]), "pixel": it["pixel"],
                                      "duration": it["duration"], "hasVideo": bool(it.get("video"))})
            return self._send(200, {"items": items})

        if len(parts) == 5 and parts[0] == "api" and parts[1] == "pending" and parts[3] == "frames":
            pid, idx = parts[2], int(parts[4].rsplit(".", 1)[0] or 0)
            with _lock:
                it = _pending.get(pid)
                frames = list(it["frames"]) if it else []
            if not it or idx < 0 or idx >= len(frames):
                return self._send(404, {"error": "not found"})
            return self._send(200, frames[idx], "image/png")

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "pending" and parts[2].endswith(".zip"):
            pid = parts[2][:-4]
            with _lock:
                it = _pending.get(pid)
                frames = list(it["frames"]) if it else []
                action = it["action"] if it else "frames"
            if not it:
                return self._send(404, {"error": "not found"})
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for i, b in enumerate(frames):
                    z.writestr(f"{action}/{i:04d}.png", b)
            fn = urllib.parse.quote(f"{action}_像素帧.zip")
            return self._send(200, buf.getvalue(), "application/zip",
                              {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})

        if path == "/file":
            p = q.get("p", [""])[0]
            if not p or not safe_path(p) or not os.path.isfile(p):
                return self._send(404, {"error": "not found"})
            ext = os.path.splitext(p)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".mp4": "video/mp4", ".json": "application/json"}.get(ext, "application/octet-stream")
            with open(p, "rb") as f:
                return self._send(200, f.read(), ctype)

        if path == "/download":
            name, action = q.get("name", [""])[0], q.get("action", [""])[0]
            fd = os.path.join(pl.OUT_ROOT, name, action, "像素帧")
            if not os.path.isdir(fd):
                return self._send(404, {"error": "no frames"})
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(os.listdir(fd)):
                    if f.endswith(".png"):
                        z.write(os.path.join(fd, f), f"{action}/{f}")
            fn = urllib.parse.quote(f"{name}_{action}_像素帧.zip")
            return self._send(200, buf.getvalue(), "application/zip",
                              {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})

        return self._send(404, {"error": "not found"})

    # ---------------- POST ----------------
    def do_POST(self):
        global _thread, _selected_cid
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/api/candidates/generate":
            body = self._json_body()
            with _lock:
                if _job["status"] == "running":
                    return self._send(409, {"error": "已有任务在运行"})
            n = int(body.get("count") or 4)
            _reset_job("", "candidates")
            _thread = threading.Thread(target=_run_candidates, args=(body, n), daemon=True)
            _thread.start()
            return self._send(200, {"ok": True})

        if u.path == "/api/candidates/select":
            body = self._json_body()
            cid = body.get("cid", "")
            with _lock:
                item = _cands.get(cid)
                if not item:
                    return self._send(404, {"error": "候选图不存在或已被回收"})
                _selected_cid = cid
                _char.update({"key": item["key"], "png": item["png"], "label": "候选图", "asset": ""})
            return self._send(200, {"ok": True})

        if u.path == "/api/candidates/drop":
            body = self._json_body()
            cid = body.get("cid", "")
            with _lock:
                item = _cands.pop(cid, None)
                if cid in _cand_order:
                    _cand_order.remove(cid)
                if _selected_cid == cid:
                    # 丢掉的就是当前在用的那张，当前角色也一并清空，避免“删了还在用”
                    _selected_cid = None
                    _char.update({"key": None, "png": None, "label": "", "asset": ""})
            if item:
                pl.mem_drop(item["key"])
            return self._send(200, {"ok": True})

        if u.path == "/api/charimage":
            # 上传角色图 -> 只进内存，不落盘
            name = q.get("name", [""])[0]
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n)
            if len(data) < 100:
                return self._send(400, {"error": "图片数据无效"})
            try:
                # 不在这里等 ComfyUI 启动：上传要立刻返回，生成时再由 _ensure_char_key 重传
                key = pl.mem_put(data, pl.new_key("char_"))
            except Exception as e:
                key = None
                print("上传暂存内存池失败（生成时会自动重试）：", e)
            with _lock:
                _selected_cid = None
                _char.update({"key": key, "png": data, "label": "本地上传", "asset": ""})
            return self._send(200, {"ok": True, "key": key})

        if u.path == "/api/char/clear":
            with _lock:
                _selected_cid = None
                _char.update({"key": None, "png": None, "label": "", "asset": ""})
            return self._send(200, {"ok": True})

        if u.path == "/api/assets/use":
            body = self._json_body()
            name = body.get("name", "")
            if not use_asset_char(name):
                return self._send(404, {"error": "该资产没有角色图（只能用候选图或上传图片）"})
            return self._send(200, {"ok": True})

        if u.path == "/api/assets/delete":
            body = self._json_body()
            name = pl.safe_name(body.get("name", ""))
            target = os.path.join(pl.OUT_ROOT, name)
            if not name or not safe_path(target) or not os.path.isdir(target):
                return self._send(404, {"error": "资产不存在"})
            with _lock:
                if _char.get("asset") == name:
                    _selected_cid = None
                    _char.update({"key": None, "png": None, "label": "", "asset": ""})
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            return self._send(200, {"ok": True})

        if u.path == "/api/generate":
            body = self._json_body()
            if not body.get("name"):
                return self._send(400, {"error": "缺少资产名称"})
            if not (body.get("actions") or []):
                return self._send(400, {"error": "请至少选择一个动作"})
            with _lock:
                if _job["status"] == "running":
                    return self._send(409, {"error": "已有任务在运行，请等待完成或先取消"})
                has_char = bool(_char.get("png"))
            if not has_char:
                return self._send(400, {"error": "还没有角色图：请先批量生成并选一张，或上传图片"})
            _reset_job(body["name"], "actions")
            _thread = threading.Thread(target=_run_actions, args=(body,), daemon=True)
            _thread.start()
            return self._send(200, {"ok": True})

        if u.path == "/api/pending/accept":
            body = self._json_body()
            pid = body.get("pid", "")
            with _lock:
                it = _pending.get(pid)
                if not it:
                    return self._send(404, {"error": "结果不存在或已被回收"})
                _pending.pop(pid, None)
                if pid in _pending_order:
                    _pending_order.remove(pid)
            try:
                if it.get("char_png"):
                    pl.save_char_image(it["name"], it["char_png"])
                pl.save_action(it["name"], it["action"], it["frames"], it["pixel"],
                               it["duration"], it.get("video"))
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": f"保存失败：{e}"})
            return self._send(200, {"ok": True, "name": it["name"], "action": it["action"]})

        if u.path == "/api/pending/drop":
            body = self._json_body()
            pid = body.get("pid", "")
            with _lock:
                _pending.pop(pid, None)
                if pid in _pending_order:
                    _pending_order.remove(pid)
            return self._send(200, {"ok": True})

        if u.path == "/api/cancel":
            _cancel_flag["v"] = True
            with _lock:
                if _job["status"] == "running":
                    _job["message"] = "正在取消…"
            return self._send(200, {"ok": True})

        if u.path == "/api/actions":
            body = self._json_body()
            return self._send(200, {"ok": True, "actions": save_action_lib(body.get("actions"))})

        if u.path == "/api/delete":
            body = self._json_body()
            name, action = pl.safe_name(body.get("name", "")), body.get("action", "")
            if not name or not action:
                return self._send(400, {"error": "缺少资产名或动作名"})
            target = os.path.join(pl.OUT_ROOT, name, action)
            if not safe_path(target) or not os.path.isdir(target):
                return self._send(404, {"error": "不存在"})
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            man = pl.load_manifest(name)
            man.get("actions", {}).pop(action, None)
            pl.save_manifest(name, man)
            return self._send(200, {"ok": True})

        # ---- 挂机模式 ----
        if u.path == "/api/hang/queue":
            body = self._json_body()
            raw = body.get("queue")
            if not isinstance(raw, list):
                return self._send(400, {"error": "queue 必须是数组"})
            q = []
            for x in raw:
                if not isinstance(x, dict):
                    continue
                n, p = str(x.get("name", "")).strip(), str(x.get("prompt", "")).strip()
                if n or p:
                    q.append({"name": n, "prompt": p})
            with _lock:
                if _hang["status"] == "running":
                    return self._send(409, {"error": "队列正在执行，先停止再改"})
                _hang["queue"] = q
            save_hang_settings()
            return self._send(200, {"ok": True, "count": len(q)})

        if u.path == "/api/hang/pickdir":
            body = self._json_body()
            cur = str(body.get("initial") or "").strip()
            path = pick_folder(cur or hang_out_dir())
            if not path:
                return self._send(200, {"ok": False, "cancelled": True})
            path = os.path.abspath(path)
            with _lock:
                _hang["outDir"] = path
            save_hang_settings()
            return self._send(200, {"ok": True, "outDir": path})

        if u.path == "/api/hang/outdir":
            body = self._json_body()
            raw = str(body.get("outDir") or "").strip()
            if not raw:
                with _lock:
                    _hang["outDir"] = ""
                save_hang_settings()
                return self._send(200, {"ok": True, "outDir": pl.OUT_ROOT})
            path = os.path.abspath(os.path.expanduser(raw))
            ok, err = _writable(path)
            if not ok:
                return self._send(400, {"error": f"目录不可写：{err}"})
            with _lock:
                _hang["outDir"] = path
            save_hang_settings()
            return self._send(200, {"ok": True, "outDir": path})

        if u.path == "/api/hang/open":
            body = self._json_body()
            d = os.path.abspath(str(body.get("dir") or "") or hang_out_dir())
            try:
                os.makedirs(d, exist_ok=True)
                os.startfile(d)          # 仅 Windows
            except Exception as e:
                return self._send(500, {"error": f"打不开：{e}"})
            return self._send(200, {"ok": True, "dir": d})

        if u.path == "/api/hang/start":
            body = self._json_body()
            with _lock:
                if _hang["status"] == "running":
                    return self._send(409, {"error": "挂机队列正在跑"})
                if _job["status"] == "running":
                    return self._send(409, {"error": "普通任务正在跑，等它结束或先取消"})
            raw = body.get("queue")
            if isinstance(raw, list):
                items = [{"name": str(x.get("name", "")).strip(), "prompt": str(x.get("prompt", "")).strip()}
                         for x in raw if isinstance(x, dict)]
            else:
                with _lock:
                    items = [dict(x) for x in _hang["queue"]]
            items = [x for x in items if x["name"] and x["prompt"]]
            if not items:
                return self._send(400, {"error": "队列为空：每条都要有资产名称和提示词"})
            acts = [str(a).strip() for a in (body.get("actions") or []) if str(a).strip()]
            if not acts:
                return self._send(400, {"error": "请先在第 2 步选好动作序列"})

            out_dir = os.path.abspath(str(body.get("outDir") or "").strip() or hang_out_dir())
            ok, err = _writable(out_dir)
            if not ok:
                return self._send(400, {"error": f"输出目录不可写：{err}"})

            params = {k: body.get(k) for k in
                      ("motion", "duration", "pixel", "video_size", "size", "steps", "keep_video")
                      if body.get(k) is not None}
            with _lock:
                _hang.update({
                    "queue": [dict(x) for x in items], "actions": acts, "outDir": out_dir,
                    "status": "running", "idx": 0, "total": len(items), "curName": "",
                    "curAction": "", "stage": "准备", "message": "准备开始…", "percent": 0,
                    "logs": [], "done": 0, "plan": len(items) * len(acts), "fail": [],
                    "started": time.time(), "finished": 0,
                })
                _hang_cancel["v"] = False
            save_hang_settings()
            threading.Thread(target=_hang_runner, args=(items, acts, params, out_dir), daemon=True).start()
            return self._send(200, {"ok": True, "plan": len(items) * len(acts), "outDir": out_dir})

        if u.path == "/api/hang/stop":
            _hang_cancel["v"] = True
            with _lock:
                if _hang["status"] == "running":
                    _hang["message"] = "正在停止…"
            return self._send(200, {"ok": True})

        if u.path == "/api/hang/clear":
            with _lock:
                if _hang["status"] == "running":
                    return self._send(409, {"error": "正在跑，先停止"})
                _hang.update({"queue": [], "status": "idle", "logs": [], "fail": [],
                              "done": 0, "plan": 0, "percent": 0, "message": "",
                              "curName": "", "curAction": "", "stage": ""})
            save_hang_settings()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def _port_busy(port, timeout=0.6):
    """显式探测端口是否已被占用。

    不能只靠 bind 时的 OSError：Windows 下 SO_REUSEADDR 允许重复绑定同一端口，
    会导致第二个服务悄悄起来抢连接，表现为“双击两次就有两个实例”。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    url = f"http://127.0.0.1:{PORT}"

    # 已经有实例在跑：只打开浏览器，然后立刻退出（退出码 2，启动脚本据此不暂停）
    if _port_busy(PORT):
        print(f"界面已经在运行，正在打开浏览器：{url}", flush=True)
        webbrowser.open(url)
        time.sleep(1.5)
        return 2

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"端口 {PORT} 无法监听：{e}", flush=True)
        return 1

    print(f"MyPixelTool 网页界面已启动: {url}", flush=True)
    print("保持本窗口打开；关闭窗口即停止服务。", flush=True)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # 后台预热 ComfyUI，省得第一次点生成还要等 1 分钟
    def _warm():
        try:
            if not pl.server_alive():
                pl.ensure_server(lambda *a: None)
        except Exception:
            pass
    threading.Timer(2.0, _warm).start()

    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
