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
_char = {"key": None, "png": None, "label": ""}   # 当前角色图（内存）
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

        if path == "/api/status":
            with _lock:
                job = dict(_job)
                job["logs"] = job["logs"][-200:]
            return self._send(200, {"comfy": pl.server_alive(), "memReady": pl.mem_nodes_ready(), "job": job})

        if path == "/api/assets":
            return self._send(200, {"assets": list_assets()})

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

        # --- 内存池：候选图 ---
        if path == "/api/candidates":
            with _lock:
                items = [{"cid": cid, "seed": _cands[cid]["seed"]} for cid in _cand_order if cid in _cands]
                sel = _selected_cid
                char = {"label": _char.get("label", ""), "hasPng": bool(_char.get("png"))}
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
                _char.update({"key": item["key"], "png": item["png"], "label": "候选图"})
            return self._send(200, {"ok": True})

        if u.path == "/api/candidates/drop":
            body = self._json_body()
            cid = body.get("cid", "")
            with _lock:
                item = _cands.pop(cid, None)
                if cid in _cand_order:
                    _cand_order.remove(cid)
                if _selected_cid == cid:
                    _selected_cid = None
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
                pl.ensure_server(lambda *a: None)
                key = pl.mem_put(data, pl.new_key("char_"))
            except Exception as e:
                key = None
                print("上传失败：", e)
            with _lock:
                _selected_cid = None
                _char.update({"key": key, "png": data, "label": "本地上传"})
            return self._send(200, {"ok": True, "key": key})

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
            return self._send(200, {"ok": True})

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

        if u.path == "/api/delete":
            body = self._json_body()
            name, action = body.get("name", ""), body.get("action", "")
            target = os.path.join(pl.OUT_ROOT, name, action) if action else os.path.join(pl.OUT_ROOT, name)
            if not safe_path(target) or not os.path.exists(target):
                return self._send(404, {"error": "不存在"})
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            if action:
                man = pl.load_manifest(name)
                man.get("actions", {}).pop(action, None)
                pl.save_manifest(name, man)
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
