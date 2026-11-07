# -*- coding: utf-8 -*-
"""
硬件体检：判断这台电脑能跑哪一档，并给出下一步该怎么做。

用法：
    python check_env.py            看中文报告
    python check_env.py --json     输出机器可读结果（给安装脚本/网页用）

判定档位：
    full    有 N 卡、显存 >= 8GB —— 全功能（出图 + 出动画）
    lite    有 N 卡、显存 4~7GB —— 能跑全功能但慢，建议降分辨率；或改用极简档只出图
    remote  没有可用的 N 卡（核显/纯 CPU）—— 本地只适合出静态图且很慢，
            推荐「一台显卡机当服务端，这台用浏览器连过去」
"""
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMFY = os.path.join(ROOT, "ComfyUI")
MANIFEST = os.path.join(ROOT, "models", "manifest.json")

# 模型放哪儿：优先 ComfyUI/models（安装后的实际位置），其次仓库里的 models 目录
MODEL_DIRS = [os.path.join(COMFY, "models"), os.path.join(ROOT, "models")]


def _run(cmd, timeout=12):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = p.stdout or b""
        return out.decode("utf-8", "ignore").strip()
    except Exception:
        return ""


# 虚拟显示器 / 远程桌面驱动，不能当成显卡
VIRTUAL_KEYS = ("idd", "virtual", "basic display", "parsec", "todesk", "sunlogin",
                "oray", "vmware", "citrix", "usb display", "displaylink")


def is_virtual_gpu(name):
    low = (name or "").lower()
    return any(k in low for k in VIRTUAL_KEYS)


def gpu_info():
    """返回 (名称, 显存GB, 驱动版本)。拿不到就返回 None"""
    raw = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits"])
    if raw:
        line = raw.splitlines()[0]
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3:
            try:
                return {"name": parts[0], "vram": round(float(parts[1]) / 1024, 1),
                        "driver": parts[2], "kind": "nvidia"}
            except ValueError:
                pass
    # 退回 WMI：能识别核显 / A 卡，但拿不到可靠显存
    if platform.system() == "Windows":
        ps = _run(["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_VideoController | "
                   "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"], timeout=20)
        try:
            data = json.loads(ps) if ps else []
            if isinstance(data, dict):
                data = [data]
            picked = None
            for d in data:
                nm = str(d.get("Name") or "")
                if not nm or is_virtual_gpu(nm):
                    continue
                # 优先挑正经的独立/集成显卡
                if picked is None or any(k in nm for k in ("NVIDIA", "AMD", "Radeon", "Intel")):
                    picked = d
                    if any(k in nm for k in ("NVIDIA", "AMD", "Radeon")):
                        break
            if picked:
                nm = str(picked.get("Name") or "")
                kind = "nvidia" if "NVIDIA" in nm else ("intel" if "Intel" in nm else
                                                        ("amd" if ("AMD" in nm or "Radeon" in nm) else "other"))
                mb = picked.get("AdapterRAM") or 0
                try:
                    mb = int(mb)
                except Exception:
                    mb = 0
                # WMI 的 AdapterRAM 是 32 位，超过 4GB 会被截断，不能当真
                gb = 0.0 if mb >= 4 * 1024 ** 3 * 0.99 else round(mb / 1024 ** 3, 1)
                return {"name": nm, "vram": gb, "driver": "", "kind": kind,
                        "noSmi": kind == "nvidia"}
        except Exception:
            pass
    return None


def ram_gb():
    if platform.system() == "Windows":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / 1024 ** 3, 1)
        except Exception:
            pass
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3, 1)
    except Exception:
        return 0


def disk_free_gb():
    try:
        return round(shutil.disk_usage(ROOT).free / 1024 ** 3, 1)
    except Exception:
        return 0


def models_state():
    """按 manifest 检查模型是否齐全。返回 (已有列表, 缺失列表, 已用GB, 需要GB)"""
    if not os.path.isfile(MANIFEST):
        return [], [], 0, 0
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            man = json.load(f)
    except Exception:
        return [], [], 0, 0
    have, miss, have_gb, need_gb = [], [], 0, 0
    for item in man.get("files", []):
        if not item.get("required", True):
            continue
        target = item.get("target", "")
        size = int(item.get("size") or 0)
        need_gb += size / 1e9          # 与发布文档的 69GB 口径一致（十进制）
        found = None
        for base in MODEL_DIRS:
            p = os.path.join(base, *target.split("/"))
            if os.path.isfile(p):
                got = os.path.getsize(p)
                # 允许 1% 误差（不同镜像可能有填充差异）
                if size and abs(got - size) > max(1024 * 1024, size * 0.01):
                    found = None
                    continue
                found = p
                break
        if found:
            have.append(target)
            have_gb += size / 1e9
        else:
            miss.append({"target": target, "label": item.get("label", target),
                         "gb": round(size / 1e9, 1)})
    return have, miss, round(have_gb, 1), round(need_gb, 1)


def recommend(gpu, ram, free_disk, missing_gb):
    """给出档位与建议"""
    vram = (gpu or {}).get("vram") or 0
    kind = (gpu or {}).get("kind") or "none"
    notes = []

    if gpu and gpu.get("noSmi"):
        return {"tier": "remote", "label": "检测到 NVIDIA 显卡，但驱动没装好", "size": "1024x1024",
                "notes": ["系统里有 N 卡，但 nvidia-smi 命令不可用 —— 驱动没装、装坏了，或版本太旧",
                          "先装最新的 NVIDIA 驱动（GeForce 官网 / NVIDIA App），重启后再双击「硬件检测.bat」",
                          "驱动装好之前，程序会当成没有显卡处理"]}

    if kind == "nvidia" and vram >= 8:
        tier, label = "full", "全功能（出图 + 出动画）"
        size = "1328x1328"
        if vram >= 12:
            notes.append("显存充裕，可以用 1536 甚至更高分辨率")
    elif kind == "nvidia" and vram >= 4:
        tier, label = "full", "能跑全功能，但需要降档"
        size = "1024x1024" if vram >= 6 else "896x896"
        notes.append(f"显存 {vram}GB 偏紧：分辨率自动降到 {size}，单段视频时长压到 2 秒内")
        notes.append("不建议同时开浏览器其他大页面，容易抢显存")
    else:
        tier, label = "remote", "本机不适合跑动画"
        size = "1024x1024"
        if kind == "intel":
            notes.append(f"检测到 Intel 显卡（{gpu['name']}），核显与内存共享，跑视频模型基本不可用")
        elif kind in ("amd", "other"):
            notes.append(f"检测到 {gpu['name']}：本工具的加速链路只针对 NVIDIA 显卡调优，A 卡/其他卡不保证能跑")
        else:
            notes.append("没有检测到可用的 NVIDIA 显卡")
        notes.append("推荐做法：找一台有 N 卡的电脑跑「服务端」，这台用浏览器连过去，功能完全一样")

    if ram and ram < 24 and tier != "remote":
        notes.append(f"系统内存只有 {ram}GB：视频模型要靠内存倒腾，建议关掉其他程序；"
                     "16GB 会很吃力，出图能跑但出动画容易失败")
    if missing_gb > 0 and free_disk < missing_gb * 1.2:
        notes.append(f"磁盘只剩 {free_disk}GB，装模型要 {missing_gb}GB，先把盘腾出来")

    return {"tier": tier, "label": label, "size": size, "notes": notes}


def collect():
    gpu = gpu_info()
    ram = ram_gb()
    free_disk = disk_free_gb()
    have, miss, have_gb, need_gb = models_state()
    missing_gb = round(need_gb - have_gb, 1)
    rec = recommend(gpu, ram, free_disk, missing_gb)
    return {
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "gpu": gpu,
        "ram_gb": ram,
        "disk_free_gb": free_disk,
        "models": {"have": len(have), "missing": miss,
                   "have_gb": have_gb, "need_gb": need_gb, "missing_gb": missing_gb,
                   "installed": len(have) > 0 and not miss},
        "recommend": rec,
    }


def report(d):
    gpu, rec = d["gpu"], d["recommend"]
    line = "=" * 62
    print(line)
    print("  Pixel Sprite Studio · 硬件体检")
    print(line)
    print(f"  系统      {d['os']}   Python {d['python']}")
    if gpu:
        vram = f"{gpu['vram']}GB 显存" if gpu["vram"] else "显存未知"
        drv = f"  驱动 {gpu['driver']}" if gpu.get("driver") else ""
        print(f"  显卡      {gpu['name']}  ({vram}){drv}")
    else:
        print("  显卡      未检测到（可能是核显或驱动异常）")
    print(f"  内存      {d['ram_gb']}GB")
    print(f"  磁盘      {d['disk_free_gb']}GB 可用（当前目录所在盘）")
    m = d["models"]
    if m["installed"]:
        print(f"  模型      齐全（{m['have_gb']}GB）")
    elif m["have"]:
        print(f"  模型      已装 {m['have']} 个 / 共 {m['have'] + len(m['missing'])} 个，还差 {m['missing_gb']}GB")
    else:
        print(f"  模型      未安装（需要下载约 {m['need_gb']}GB）")
    print(line)
    print(f"  结论      {rec['label']}")
    print(f"  建议分辨率 {rec['size']}")
    for n in rec["notes"]:
        print(f"            · {n}")
    print(line)
    if m["missing"]:
        print("  还缺这些模型：")
        for x in m["missing"]:
            print(f"    - {x['gb']:>5.1f}GB  {x['label']}")
        print("  双击「下载模型.bat」会自动补齐（支持断点续传）。")
        print(line)
    if rec["tier"] == "remote":
        print("  下一步：")
        print("    1. 在另一台有 NVIDIA 显卡的电脑上装好本工具，双击「启动服务端(局域网).bat」")
        print("    2. 那台机器会显示一个 http://192.168.x.x:8899 的地址，抄下来")
        print("    3. 在本机浏览器里打开这个地址，就能用全部功能（含动画）")
        print("    4. 如果想在本机先试试静态图：双击「启动网页界面.bat」，选「只出像素图」，")
        print("       一张图大约 3~8 分钟（会比显卡机慢很多，属正常）")
    elif rec["tier"] == "full":
        print("  下一步：双击「一键安装.bat」，装完双击「启动网页界面.bat」即可。")
    print(line)


def main():
    if "--json" in sys.argv:
        print(json.dumps(collect(), ensure_ascii=False, indent=2))
        return 0
    try:
        if platform.system() == "Windows":
            os.system("chcp 65001 >nul 2>&1")
    except Exception:
        pass
    report(collect())
    return 0


if __name__ == "__main__":
    sys.exit(main())
