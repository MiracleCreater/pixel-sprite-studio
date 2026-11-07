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

_env_cache = {"t": 0.0, "data": None}


def env_info():
    """硬件体检 + 模型档位（结果缓存 30 秒，避免每次都跑 nvidia-smi）"""
    now = time.time()
    with _lock:
        cached = _env_cache["data"]
        fresh = cached is not None and (now - _env_cache["t"] < 30)
    if fresh:
        data = json.loads(json.dumps(cached, ensure_ascii=False))
    else:
        try:
            import check_env
            data = check_env.collect()
        except Exception as e:
            traceback.print_exc()
            data = {"error": str(e)}
        with _lock:
            _env_cache["data"] = data
            _env_cache["t"] = now
        data = json.loads(json.dumps(data, ensure_ascii=False))
    data["profile"] = pl.model_profile()
    return data

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
    # 待执行队列：跑完一条就从队头摘掉，运行中可随时往队尾追加
    # 条目结构 {"name","prompt","acts":[...]|None,"mode":"gen"|"actions"}
    #   mode=gen      ：文生图出角色图 → 跑动作（acts 为空则用全局动作序列）
    #   mode=actions  ：沿用硬盘上已有的角色图，只补做 acts 里的动作
    "queue": [],
    "outDir": "",         # 输出根目录（空 = 默认 MyPixelTool\输出）
    "actions": [],        # 默认动作序列（队尾新条目没单独指定动作时用它）
    "status": "idle",     # idle | running | done | cancelled | error
    "curName": "",
    "curAction": "",
    "stage": "",
    "message": "",
    "percent": 0,
    "logs": [],
    "done": 0,            # 本轮已完成动作数
    "fail": [],           # [{"name","error"}]
    "recent": [],         # 本轮已入册：[{"name","actions","ts"}]
    "started": 0,
    "finished": 0,
    "params": {},         # 本轮渲染参数（追加的条目沿用）
    "unitsDone": 0,       # 进度单元：出图 1 个 + 每个动作 1 个
    "pctDrop": False,     # 刚追加过任务 → 允许进度条退一点（总工作量变大了）
}
_hang_cancel = {"v": False}


def _split_acts(v):
    """把 "站立，行走, 攻击" 或 ["站立","行走"] 统一成动作名列表"""
    if isinstance(v, list):
        return [str(a).strip() for a in v if str(a).strip()]
    s = str(v or "")
    for sep in ("，", "、", "；", ";", "|", "/", "　", " "):
        s = s.replace(sep, ",")
    return [x.strip() for x in s.split(",") if x.strip()]


def _norm_item(x, strict=True):
    """规范化一条队列任务；不合法返回 None。strict=False 时允许名称/提示词暂时为空（编辑中）

    队列单元 = 三元组（资产名称, 图片描述, k 个动作序列）；acts 为空表示沿用默认动作序列。
    """
    if not isinstance(x, dict):
        return None
    name = str(x.get("name", "")).strip()
    prompt = str(x.get("prompt", "")).strip()
    mode = "actions" if x.get("mode") == "actions" else "gen"
    raw = x.get("acts")
    acts = _split_acts(raw) if raw is not None else None
    acts = acts or None
    if not strict:
        if not (name or prompt):
            return None
        return {"name": name, "prompt": prompt, "acts": acts, "mode": mode}
    if not name:
        return None
    if mode == "actions":
        return {"name": name, "prompt": prompt, "acts": acts, "mode": mode} if acts else None
    if not prompt:
        return None
    return {"name": name, "prompt": prompt, "acts": acts, "mode": mode}


def _item_acts(it, default_acts):
    acts = it.get("acts")
    if acts is None:
        acts = default_acts
    return [str(a).strip() for a in (acts or []) if str(a).strip()]


def _item_units(it, default_acts):
    n = len(_item_acts(it, default_acts))
    if it.get("mode") == "actions":
        return max(1, n)
    # 不选任何动作时 = 生成角色图 + 像素图，共 2 个单元
    return n + 2 if n == 0 else n + 1


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
        it = _norm_item(x, strict=False)
        if it:
            q.append(it)
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
    """把当前单元内部的 0~100 进度折算成整队列的百分比。

    队列会边跑边长，所以每次都用「已完成单元 + 剩下队列的单元」现算分母。
    """
    with _lock:
        ga = _hang["actions"]
        pend = sum(_item_units(x, ga) for x in _hang["queue"])
        denom = max(1.0, float(_hang["unitsDone"] + pend))
        sub = max(0.0, min(1.0, (int(percent) if percent else 0) / 100.0))
        overall = (_hang["unitsDone"] + sub) / denom * 100
        _hang["stage"] = stage
        _hang["message"] = msg
        if _hang["pctDrop"]:          # 刚追加过任务，允许回落一次
            _hang["percent"] = max(0, min(99, int(overall)))
            _hang["pctDrop"] = False
        else:
            _hang["percent"] = min(99, max(_hang["percent"], int(overall)))
        _hang["logs"].append({"t": time.strftime("%H:%M:%S"), "stage": stage, "msg": msg})
        if len(_hang["logs"]) > 600:
            del _hang["logs"][:200]


def _hang_run_one(it, params, out_dir):
    """跑一条任务：出角色图（或沿用硬盘上的）→ 逐个动作生成、立即落盘。

    返回 (ok, acts 或错误信息)。取消时向上抛 RuntimeError('已取消')。
    """
    with _lock:
        ga = list(_hang["actions"])
        name = (it.get("name") or "").strip() or "未命名"
        prompt = (it.get("prompt") or "").strip()
        mode = it.get("mode") or "gen"
        acts = _item_acts(it, ga)
        _hang.update({"curName": name, "curAction": "",
                      "stage": "载入角色图" if mode == "actions" else "文生图",
                      "message": f"{name} · " + ("沿用硬盘角色图" if mode == "actions" else "生成角色图")})
    key = None
    try:
        if mode == "actions":
            p = os.path.join(out_dir, pl.safe_name(name), "角色图.png")
            if not os.path.isfile(p):
                raise RuntimeError("找不到 角色图.png，无法补做动作")
            with open(p, "rb") as f:
                png = f.read()
            key = pl.mem_put(png, pl.new_key("char_"))
            _hang_log(f"↻ {name} · 沿用硬盘上的角色图（补做 {len(acts)} 个动作）", "载入")
        else:
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
            with _lock:
                _hang["unitsDone"] += 1
            _hang_log(f"✓ {name} · 角色图已存盘", "角色")

        for act in acts:
            if _hang_cancel["v"]:
                raise RuntimeError("已取消")
            with _lock:
                _hang.update({"curAction": act, "stage": "图生视频", "message": f"{name} · {act}"})
            res = pl.render_actions({**params, "name": name}, key, [act],
                                    _hang_emit, lambda: _hang_cancel["v"])
            r = res[0]
            pl.save_action(name, act, r["frames"], r["pixel"], r["duration"],
                           r.get("video"), root=out_dir)
            with _lock:
                _hang["done"] += 1
                _hang["unitsDone"] += 1
            _hang_log(f"✓ {name} · {act} 已存盘（{len(r['frames'])} 帧）", "完成")

        if not acts and mode != "actions":
            # 没选动作：只要一张像素图。角色图已在上面存好，这里直接抠图 + 像素化
            if _hang_cancel["v"]:
                raise RuntimeError("已取消")
            with _lock:
                _hang.update({"curAction": pl.PIXEL_IMAGE, "stage": "像素化",
                              "message": f"{name} · 生成像素图"})
            px = int(params.get("pixel") or 64)
            out = pl.pixelize_image(key, px, _hang_emit, lambda: _hang_cancel["v"])
            pl.save_pixel_image(name, out, px, root=out_dir)
            with _lock:
                _hang["done"] += 1
                _hang["unitsDone"] += 1
            _hang_log(f"✓ {name} · 像素图已存盘", "完成")
        return True, acts
    finally:
        if key:
            try:
                pl.mem_drop(key)
            except Exception:
                pass


def _hang_runner(params, out_dir):
    """消费式跑法：队列里取队头 → 跑完立刻摘掉 → 队列空了收工。

    运行中随时可以往队尾追加（前端 /api/hang/queue 的 append），
    每轮循环都重新看队列，所以追加的活会接着干。
    """
    try:
        pl.ensure_server(_hang_emit)
        while True:
            with _lock:
                empty = not _hang["queue"]
            if empty:
                # 收尾前稍等一下：最后一条刚跑完的瞬间你可能正好又追加了
                time.sleep(0.5)
                with _lock:
                    empty = not _hang["queue"]
                if empty:
                    break
            if _hang_cancel["v"]:
                raise RuntimeError("已取消")
            with _lock:
                it = _hang["queue"][0]
                na = (it.get("name") or "").strip() or "未命名"
                _hang["message"] = f"队列剩 {len(_hang['queue'])} 条 · 正在处理「{na}」"
            ok, info = False, ""
            try:
                ok, info = _hang_run_one(it, params, out_dir)
            except RuntimeError as e:
                if "已取消" in str(e):
                    raise
                ok, info = False, str(e)
            except Exception as e:
                traceback.print_exc()
                ok, info = False, str(e)

            with _lock:
                if _hang["queue"] and _hang["queue"][0] is it:
                    _hang["queue"].pop(0)
                if ok:
                    _hang["recent"].append({"name": it.get("name"), "actions": info, "ts": time.time()})
                else:
                    _hang["fail"].append({"name": it.get("name"), "error": info})
            save_hang_settings()
            if ok:
                _hang_log(f"→ {it.get('name')} 已移出队列，进「已完成」", "入册")
            else:
                _hang_log(f"✗ {it.get('name')} 失败：{info}（已跳过）", "错误")

        with _lock:
            fails = len(_hang["fail"])
            _hang.update({"status": "done", "percent": 100, "stage": "完成",
                          "message": f"队列跑完：成功 {_hang['done']} 个动作"
                                     + (f"，{fails} 条失败" if fails else "") + "，可以到「已完成」里过一眼",
                          "curAction": ""})
    except Exception as e:
        cancelled = _hang_cancel["v"] or "已取消" in str(e)
        with _lock:
            _hang.update({"status": "cancelled" if cancelled else "error",
                          "stage": "结束", "curAction": "",
                          "message": ("已停止；没跑完的那条还留在队头" if cancelled else f"出错：{e}")})
        if not cancelled:
            traceback.print_exc()
    finally:
        with _lock:
            _hang["finished"] = time.time()
        save_hang_settings()


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

def list_assets(root=None):
    """扫某个输出根目录下的资产（挂机可能写到别的盘，所以 root 可指定）"""
    base = root or pl.OUT_ROOT
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        if not os.path.isdir(d) or name.startswith("_"):
            continue
        man = pl.load_manifest(name, base)
        acts, seen = [], set()
        # 静态像素图排在最前（它是这张脸的“证件照”）
        if os.path.exists(os.path.join(d, pl.PIXEL_IMAGE + ".png")):
            acts.append({"action": pl.PIXEL_IMAGE, "frames": 1})
            seen.add(pl.PIXEL_IMAGE)
        for a, info in man.get("actions", {}).items():
            fd = os.path.join(d, a, "像素帧")
            if os.path.isdir(fd):
                acts.append({"action": a, "frames": len([f for f in os.listdir(fd) if f.endswith(".png")])})
                seen.add(a)
        for a in sorted(os.listdir(d)):
            fd = os.path.join(d, a, "像素帧")
            if a not in seen and os.path.isdir(fd):
                acts.append({"action": a, "frames": len([f for f in os.listdir(fd) if f.endswith(".png")])})
        try:
            mtime = os.path.getmtime(d)
        except Exception:
            mtime = 0
        out.append({
            "name": name,
            "actions": acts,
            "hasChar": os.path.exists(os.path.join(d, "角色图.png")),
            "prompt": man.get("prompt", ""),
            "mtime": mtime,
        })
    out.sort(key=lambda x: -x["mtime"])
    return out


def frame_list(name, action, root=None):
    base = root or pl.OUT_ROOT
    if action == pl.PIXEL_IMAGE:
        p = os.path.join(base, pl.safe_name(name), pl.PIXEL_IMAGE + ".png")
        if not os.path.isfile(p):
            return []
        man = pl.load_manifest(name, base)
        pixel = (man.get("pixelImage") or {}).get("pixel", 64)
        return [{"url": "/file?p=" + urllib.parse.quote(p), "pixel": pixel}]
    fd = os.path.join(base, name, action, "像素帧")
    if not os.path.isdir(fd):
        return []
    files = sorted(f for f in os.listdir(fd) if f.endswith(".png"))
    man = pl.load_manifest(name, base)
    pixel = man.get("actions", {}).get(action, {}).get("pixel", 64)
    return [{"url": "/file?p=" + urllib.parse.quote(os.path.join(fd, f)), "pixel": pixel} for f in files]


def _rm_tree(path):
    """删目录/文件并确认真的删掉了；返回 (ok, 错误信息)。

    只读文件、被占用文件会尝试解除只读后重试；失败时把原因带回去，
    避免界面点了删除却毫无反馈。
    """
    import shutil
    if not os.path.exists(path):
        return True, ""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=False)
        else:
            os.remove(path)
    except PermissionError:
        try:
            if os.path.isdir(path):
                for r, ds, fs in os.walk(path):
                    for f in fs:
                        try:
                            os.chmod(os.path.join(r, f), 0o666)
                        except Exception:
                            pass
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.chmod(path, 0o666)
                os.remove(path)
        except Exception as e:
            return False, f"文件被占用或无权限：{e}"
    except Exception as e:
        return False, str(e)
    if os.path.exists(path):
        return False, "文件仍被占用（可能正在预览或被其他程序打开）"
    return True, ""


def _allowed_roots():
    """允许读取/下载的目录：安装目录 + 默认输出 + 当前挂机输出目录"""
    roots = {os.path.abspath(pl.ROOT), os.path.abspath(pl.OUT_ROOT)}
    with _lock:
        d = (_hang.get("outDir") or "").strip()
    if d:
        roots.add(os.path.abspath(d))
    return roots


def safe_path(p, roots=None):
    p = os.path.abspath(p)
    for r in (roots or _allowed_roots()):
        if p == r or p.startswith(r + os.sep):
            return True
    return False


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
                ga = list(_hang["actions"])
                q = [dict(x) for x in _hang["queue"]]
                recent = [r.get("name") for r in _hang["recent"]]
            st["logs"] = st["logs"][-250:]
            st["running"] = st["status"] == "running"
            st["queue"] = q
            st["remaining"] = len(q)
            st["plan"] = st["done"] + sum(len(_item_acts(x, ga)) for x in q)
            st["recentNames"] = recent
            for k in ("unitsDone", "pctDrop", "params"):
                st.pop(k, None)
            st["outDirResolved"] = hang_out_dir()
            st["outDirDefault"] = pl.OUT_ROOT
            st["defaultExists"] = os.path.isdir(pl.OUT_ROOT)
            return self._send(200, st)

        if path == "/api/hang/done":
            root = os.path.abspath(q.get("root", [""])[0] or hang_out_dir())
            if not safe_path(root):
                return self._send(403, {"error": "这个目录不在允许范围"})
            items = list_assets(root)
            with _lock:
                recent = [r.get("name") for r in _hang["recent"]]
            for x in items:
                x["isNew"] = x["name"] in recent
            return self._send(200, {"items": items, "outDir": root, "recent": recent})

        if path == "/api/status":
            with _lock:
                job = dict(_job)
                job["logs"] = job["logs"][-200:]
            return self._send(200, {"comfy": pl.server_alive(), "memReady": pl.mem_nodes_ready(),
                                    "job": job, "profile": pl.model_profile()})

        if path == "/api/env":
            return self._send(200, env_info())

        if path == "/api/assets":
            return self._send(200, {"assets": list_assets(), "actions": load_action_lib()})

        if path == "/api/actions":
            return self._send(200, {"actions": load_action_lib()})

        if path == "/api/frames":
            root = q.get("root", [""])[0] or None
            if root:
                root = os.path.abspath(root)
                if not safe_path(root):
                    return self._send(403, {"error": "目录不在允许范围"})
            return self._send(200, {"frames": frame_list(q.get("name", [""])[0],
                                                         q.get("action", [""])[0], root)})

        if path == "/api/manifest":
            return self._send(200, pl.load_manifest(q.get("name", [""])[0]))

        if path == "/api/char":
            name = q.get("name", [""])[0]
            root = q.get("root", [""])[0] or pl.OUT_ROOT
            p = os.path.join(os.path.abspath(root), pl.safe_name(name), "角色图.png")
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
            root = os.path.abspath(q.get("root", [""])[0] or pl.OUT_ROOT)
            if action == pl.PIXEL_IMAGE:
                p = os.path.join(root, pl.safe_name(name), pl.PIXEL_IMAGE + ".png")
                if not safe_path(p) or not os.path.isfile(p):
                    return self._send(404, {"error": "no pixel image"})
                fn = urllib.parse.quote(f"{name}_{pl.PIXEL_IMAGE}.png")
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/png",
                                      {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
            fd = os.path.join(root, pl.safe_name(name), pl.safe_name(action), "像素帧")
            if not safe_path(fd) or not os.path.isdir(fd):
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
            root = os.path.abspath(body.get("root") or pl.OUT_ROOT)
            target = os.path.join(root, name)
            if not name or not safe_path(target) or not os.path.isdir(target):
                return self._send(404, {"error": "资产不存在"})
            with _lock:
                if _char.get("asset") == name:
                    _selected_cid = None
                    _char.update({"key": None, "png": None, "label": "", "asset": ""})
            ok, err = _rm_tree(target)
            if not ok:
                return self._send(500, {"error": f"删除失败：{err}"})
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
            root = os.path.abspath(body.get("root") or pl.OUT_ROOT)
            if not name or not action:
                return self._send(400, {"error": "缺少资产名或动作名"})
            if action == pl.PIXEL_IMAGE:
                # 静态像素图是单个文件，不是目录
                target = os.path.join(root, name, pl.PIXEL_IMAGE + ".png")
                if not safe_path(target) or not os.path.isfile(target):
                    return self._send(404, {"error": "不存在"})
            else:
                target = os.path.join(root, name, action)
                if not safe_path(target) or not os.path.isdir(target):
                    return self._send(404, {"error": "不存在"})
            ok, err = _rm_tree(target)
            if not ok:
                return self._send(500, {"error": f"删除失败：{err}"})
            man = pl.load_manifest(name, root)
            man.get("actions", {}).pop(action, None)
            if action == pl.PIXEL_IMAGE:
                man.pop("pixelImage", None)
            pl.save_manifest(name, man, root)
            return self._send(200, {"ok": True})

        if u.path == "/api/pixelize":
            # 把当前角色图（候选/上传/已保存资产）直接抠图 + 像素化，存 <资产>/像素图.png
            body = self._json_body()
            name = (body.get("name") or "").strip()
            if not name:
                return self._send(400, {"error": "请先填资产名称"})
            if _job["status"] == "running":
                return self._send(409, {"error": "已有任务在运行，请等待完成或先取消"})
            with _lock:
                png = _char.get("png")
            if not png:
                return self._send(400, {"error": "还没有角色图：先批量出图选一张，或上传一张"})
            try:
                pixel = max(16, min(256, int(body.get("pixel") or 64)))
            except Exception:
                pixel = 64
            _reset_job(name, "pixelize")
            key = pl.mem_put(png, pl.new_key("char_"))
            try:
                out = pl.pixelize_image(key, pixel, _emit, lambda: _cancel_flag["v"])
            except RuntimeError as e:
                with _lock:
                    _job.update({"status": "error", "message": str(e)})
                return self._send(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                with _lock:
                    _job.update({"status": "error", "message": f"像素化失败：{e}"})
                return self._send(500, {"error": f"像素化失败：{e}"})
            finally:
                try:
                    pl.mem_drop(key)
                except Exception:
                    pass
            p = pl.save_pixel_image(name, out, pixel)
            with _lock:
                _job.update({"status": "done", "percent": 100, "stage": "完成",
                             "message": f"像素图已存到 {os.path.basename(name)}/{pl.PIXEL_IMAGE}.png"})
            return self._send(200, {"ok": True, "path": p, "pixel": pixel})

        # ---- 挂机模式 ----
        if u.path == "/api/hang/queue":
            body = self._json_body()
            with _lock:
                running = _hang["status"] == "running"

            # ① 往队尾追加（运行中也允许）——这是挂机模式的主用法
            if "append" in body:
                add = [x for x in (_norm_item(y) for y in (body.get("append") or [])) if x]
                if not add:
                    return self._send(400, {"error": "没有可加入的条目（名称/提示词要填全）"})
                with _lock:
                    _hang["queue"].extend(add)
                    if _hang["status"] == "running":
                        _hang["pctDrop"] = True       # 总活变多，允许进度条退一点
                        _hang["message"] = f"又追加了 {len(add)} 条，接着跑"
                    n = len(_hang["queue"])
                save_hang_settings()
                return self._send(200, {"ok": True, "added": len(add), "count": n})

            # ② 删掉队列里某一条
            if "remove" in body:
                try:
                    i = int(body.get("remove"))
                except Exception:
                    return self._send(400, {"error": "remove 必须是下标"})
                with _lock:
                    if running and i == 0:
                        return self._send(409, {"error": "这条正在生成，删不了；想中断点「停止」"})
                    if 0 <= i < len(_hang["queue"]):
                        _hang["queue"].pop(i)
                    n = len(_hang["queue"])
                save_hang_settings()
                return self._send(200, {"ok": True, "count": n})

            # ③ 就地改一条（改名/改提示词）
            if "patch" in body:
                p = body.get("patch") or {}
                try:
                    i = int(p.get("i"))
                except Exception:
                    return self._send(400, {"error": "patch.i 必须是下标"})
                with _lock:
                    if not (0 <= i < len(_hang["queue"])):
                        return self._send(404, {"error": "下标越界"})
                    if running and i == 0:
                        return self._send(409, {"error": "这条正在生成，先别改"})
                    it = _hang["queue"][i]
                    if "name" in p:
                        it["name"] = str(p.get("name") or "").strip()
                    if "prompt" in p:
                        it["prompt"] = str(p.get("prompt") or "").strip()
                    if "acts" in p:
                        it["acts"] = _split_acts(p.get("acts")) or None
                    if "mode" in p and p.get("mode") in ("gen", "actions"):
                        it["mode"] = p.get("mode")
                save_hang_settings()
                return self._send(200, {"ok": True})

            # ④ 整体替换（只在空闲时）
            raw = body.get("queue")
            if not isinstance(raw, list):
                return self._send(400, {"error": "queue 必须是数组"})
            if running:
                return self._send(409, {"error": "队列正在执行：点「＋ 新增」往队尾追加"})
            q = [x for x in (_norm_item(y, strict=False) for y in raw if isinstance(y, dict)) if x]
            with _lock:
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
            acts = [str(a).strip() for a in (body.get("actions") or []) if str(a).strip()]
            with _lock:
                if isinstance(raw, list):
                    _hang["queue"] = [x for x in (_norm_item(y) for y in raw if isinstance(y, dict)) if x]
                _hang["actions"] = acts      # 空数组也照存：不选动作 = 每条只出像素图
                ga = list(_hang["actions"])
                items = [dict(x) for x in _hang["queue"]]
            if not items:
                return self._send(400, {"error": "队列为空：每条都要有资产名称和提示词"})
            no_act = [x.get("name") for x in items
                      if x.get("mode") == "actions" and not _item_acts(x, ga)]
            if no_act:
                return self._send(400, {"error": f"这些补做条目没有动作：{'、'.join(no_act[:4])}；"
                                                 "补做至少要选一个动作"})

            out_dir = os.path.abspath(str(body.get("outDir") or "").strip() or hang_out_dir())
            ok, err = _writable(out_dir)
            if not ok:
                return self._send(400, {"error": f"输出目录不可写：{err}"})

            params = {k: body.get(k) for k in
                      ("motion", "duration", "pixel", "video_size", "size", "steps", "keep_video")
                      if body.get(k) is not None}
            with _lock:
                _hang.update({
                    "outDir": out_dir, "params": params,
                    "status": "running", "curName": "", "curAction": "",
                    "stage": "准备", "message": f"准备开始，队列 {len(items)} 条",
                    "percent": 0, "pctDrop": False, "logs": [], "done": 0,
                    "unitsDone": 0, "fail": [], "recent": [],
                    "started": time.time(), "finished": 0,
                })
                _hang_cancel["v"] = False
            save_hang_settings()
            threading.Thread(target=_hang_runner, args=(params, out_dir), daemon=True).start()
            plan = sum(_item_units(x, ga) for x in items)
            return self._send(200, {"ok": True, "plan": plan, "queue": len(items), "outDir": out_dir})

        if u.path == "/api/hang/stop":
            _hang_cancel["v"] = True
            with _lock:
                if _hang["status"] == "running":
                    _hang["message"] = "正在停止…手上这一步做完就停，没跑完的那条留在队头"
            return self._send(200, {"ok": True})

        if u.path == "/api/hang/clear":
            # 运行中：只清掉还没开始的，当前这条跑完自动收工
            with _lock:
                running = _hang["status"] == "running"
                keep = [_hang["queue"][0]] if (running and _hang["queue"]) else []
                dropped = max(0, len(_hang["queue"]) - len(keep))
                _hang["queue"] = keep
                if not running:
                    _hang.update({"status": "idle", "logs": [], "fail": [], "done": 0,
                                  "unitsDone": 0, "percent": 0, "message": "",
                                  "curName": "", "curAction": "", "stage": "", "recent": []})
                else:
                    _hang["message"] = f"已清空剩余 {dropped} 条，当前这条跑完就停"
            save_hang_settings()
            return self._send(200, {"ok": True, "dropped": dropped})

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


def _lan_ips():
    """列出本机局域网 IPv4（排除回环与虚拟网卡常见的 169.254 段）"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))       # 不会真的发包，只为拿到出口网卡地址
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Pixel Sprite Studio 网页界面")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；127.0.0.1 只允许本机，0.0.0.0 允许局域网访问")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--lan", action="store_true", help="等价于 --host 0.0.0.0（局域网共享）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args, _ = ap.parse_known_args()

    host = "0.0.0.0" if args.lan else args.host
    port = args.port
    local_url = f"http://127.0.0.1:{port}"

    os.makedirs(WEB_DIR, exist_ok=True)

    # 已经有实例在跑：只打开浏览器，然后立刻退出（退出码 2，启动脚本据此不暂停）
    if _port_busy(port):
        print(f"界面已经在运行，正在打开浏览器：{local_url}", flush=True)
        if not args.no_browser:
            webbrowser.open(local_url)
        time.sleep(1.5)
        return 2

    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"端口 {port} 无法监听：{e}", flush=True)
        return 1

    print("", flush=True)
    print("=" * 58, flush=True)
    if host in ("0.0.0.0", ""):
        print("  局域网共享模式已开启（同一 WiFi / 网线下的设备都能访问）", flush=True)
        print("", flush=True)
        print(f"  本机访问    {local_url}", flush=True)
        for ip in _lan_ips():
            print(f"  其他设备    http://{ip}:{port}", flush=True)
        print("", flush=True)
        print("  别人怎么用：把上面那行 http://192.168... 地址发给他，", flush=True)
        print("  用浏览器（手机、平板、笔记本都行）打开即可，什么都不用装。", flush=True)
        print("", flush=True)
        print("  提示：首次启动 Windows 可能弹「防火墙」窗口，要点「允许访问」，", flush=True)
        print("        否则别人的设备连不上。同一局域网内的人也能看到/删除你的资产。", flush=True)
    else:
        print(f"  网页界面已启动：{local_url}", flush=True)
        print("  只有本机能访问。想让局域网其他设备用，请双击「启动服务端(局域网).bat」", flush=True)
    print("=" * 58, flush=True)
    print("保持本窗口打开；关闭窗口即停止服务。", flush=True)
    print("", flush=True)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(local_url)).start()

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
