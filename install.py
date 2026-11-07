# -*- coding: utf-8 -*-
"""一键安装脚本：把仓库变成可运行状态。

做的事情：
  1. 克隆 ComfyUI（固定版本，不把第三方代码放进本仓库）
  2. 安装本项目的自定义节点 custom_nodes/pixel_memory（结果只存内存，不落盘）
  3. 安装 Python 依赖（ComfyUI requirements + Pillow + PyAV）
  4. 打印模型清单与放置位置

用法：
    python install.py                     # 默认安装
    python install.py --comfy-ref master  # 指定 ComfyUI 分支/标签
    python install.py --skip-deps         # 只拉代码，不装依赖
    python install.py --install-torch     # 顺带安装 CUDA 版 PyTorch（约 3GB）
    python install.py --link-models       # 把 models/ 下的目录联接进 ComfyUI\\models
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_DIR = os.path.join(HERE, "ComfyUI")
NODE_SRC = os.path.join(HERE, "custom_nodes", "pixel_memory")
NODE_DST = os.path.join(COMFY_DIR, "custom_nodes", "pixel_memory")
COMFY_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
MANIFEST = os.path.join(HERE, "models", "manifest.json")
DOWNLOADER = os.path.join(HERE, "download_models.py")

DEFAULT_REF = "v0.42.0"          # 本工具适配过的版本
TORCH_CU130 = "https://download.pytorch.org/whl/cu130"


def load_models():
    """从 models/manifest.json 读清单，保证安装提示与下载脚本永远一致。"""
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f).get("files", [])
    except Exception:
        return []



def run(cmd, **kw):
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, check=False, **kw)


def step(msg):
    print("\n>>> " + msg)


def python_ok():
    if sys.version_info < (3, 10):
        print(f"[错误] 需要 Python 3.10 以上，当前 {sys.version.split()[0]}")
        return False
    print(f"Python {sys.version.split()[0]}  OK")
    return True


def get_comfy(ref):
    if os.path.isfile(os.path.join(COMFY_DIR, "main.py")):
        print("ComfyUI 已存在，跳过克隆")
        return True
    if shutil.which("git") is None:
        print("[错误] 未找到 git，请先安装 Git 或手动下载 ComfyUI 放到本目录的 ComfyUI\\")
        return False
    if os.path.exists(COMFY_DIR):
        shutil.rmtree(COMFY_DIR, ignore_errors=True)

    print(f"克隆 ComfyUI（{ref}）…约 100MB")
    base = ["git", "clone", "--depth", "1"]
    if run(base + ["--branch", ref, COMFY_REPO, COMFY_DIR]).returncode != 0:
        print(f"[提示] 标签 {ref} 拉取失败，改用默认分支")
        shutil.rmtree(COMFY_DIR, ignore_errors=True)
        if run(base + [COMFY_REPO, COMFY_DIR]).returncode != 0:
            print("[错误] ComfyUI 克隆失败，请检查网络或代理")
            return False
    return True


def install_node():
    if not os.path.isfile(os.path.join(NODE_SRC, "__init__.py")):
        print("[错误] 仓库里缺少 custom_nodes/pixel_memory")
        return False
    os.makedirs(os.path.dirname(NODE_DST), exist_ok=True)
    if os.path.exists(NODE_DST):
        shutil.rmtree(NODE_DST, ignore_errors=True)
    shutil.copytree(NODE_SRC, NODE_DST)
    print(f"已安装内存节点 -> {os.path.relpath(NODE_DST, HERE)}")
    return True


def install_deps(skip=False, torch_index=None, torch_flag=False):
    if skip:
        print("已跳过依赖安装")
        return True
    req = os.path.join(COMFY_DIR, "requirements.txt")
    if not os.path.isfile(req):
        print("[警告] 找不到 ComfyUI/requirements.txt，跳过")
        return True

    print("安装 ComfyUI 依赖（首次较久，可能 3~10 分钟）…")
    # 先装 torch，避免 pip 从默认源拉到 CPU 版
    has_torch = subprocess.run([sys.executable, "-c", "import torch"],
                               capture_output=True).returncode == 0
    if torch_flag and not has_torch:
        idx = torch_index or TORCH_CU130
        print(f"安装 CUDA 版 PyTorch（--index-url {idx}）…约 3GB")
        run([sys.executable, "-m", "pip", "install", "torch", "torchvision", "torchaudio",
             "--index-url", idx])
    elif not has_torch:
        print("[提示] 未检测到 PyTorch。若稍后启动报 No module named 'torch'，"
              "请重跑：python install.py --install-torch")

    run([sys.executable, "-m", "pip", "install", "-r", req])
    run([sys.executable, "-m", "pip", "install", "pillow", "av"])   # 本工具自身需要
    return True


def link_models():
    script = os.path.join(HERE, "MyPixelTool", "setup_models.py")
    if not os.path.isfile(script):
        print("[警告] 找不到 MyPixelTool/setup_models.py")
        return
    run([sys.executable, "-s", script, "--yes"])


def download_models(extra=None):
    if not os.path.isfile(DOWNLOADER):
        print("[错误] 找不到 download_models.py")
        return False
    cmd = [sys.executable, "-u", DOWNLOADER] + (extra or [])
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd).returncode == 0


def print_models():
    files = load_models()
    print("\n模型清单（放到 ComfyUI/models/<目录>/ 下，或放在本仓库 models/<目录>/ 后执行 --link-models）")
    print("-" * 84)
    if files:
        for it in files:
            mark = "*" if it.get("required", True) else " "
            size = it.get("size", 0) / 1e9
            print(f" {mark} {it['target']:<66} {size:>6.1f} GB  {it.get('label', '')}")
    else:
        print("  （读取 models/manifest.json 失败，请检查文件是否存在）")
    print("-" * 84)
    print("  两个档位，按电脑配置选：")
    print("    极简档 lite  约 15 GB  只出静态像素图，4~6GB 显存可跑")
    print("    全量档 full  约 69 GB  出图 + 出动画，8GB 显存起")
    print("  自动选档下载：python download_models.py --tier auto")
    print("  强制某档：    python download_models.py --tier lite / --tier full")
    print("  只检查：      python download_models.py --check")
    print("  模型版权归各作者，本仓库不附带；RMBG 2.0 为非商用许可，详见 models/manifest.json")


def main():
    ap = argparse.ArgumentParser(description="安装 Pixel Sprite Studio")
    ap.add_argument("--comfy-ref", default=DEFAULT_REF, help="ComfyUI 分支或标签（默认 %s）" % DEFAULT_REF)
    ap.add_argument("--skip-deps", action="store_true", help="跳过 pip 依赖安装")
    ap.add_argument("--install-torch", action="store_true", help="安装 CUDA 版 PyTorch")
    ap.add_argument("--torch-index", default=None, help="PyTorch 源，如 cu128/cu130")
    ap.add_argument("--link-models", action="store_true", help="把 models/ 联接进 ComfyUI/models")
    ap.add_argument("--download-models", action="store_true", help="顺带下载缺失的模型（可断点续传）")
    ap.add_argument("--tier", choices=["auto", "lite", "full"], default="auto",
                    help="模型档位：lite 约15GB只出图 / full 约69GB含动画 / auto 按显存自动选")
    ap.add_argument("--check-models", action="store_true", help="只检查模型是否齐全")
    a = ap.parse_args()

    print("=" * 84)
    print("  Pixel Sprite Studio · 安装")
    print("=" * 84)

    if a.check_models:
        step("检查模型")
        return 0 if download_models(["--check", "--tier", a.tier]) else 1

    step("检查 Python")
    if not python_ok():
        return 1

    step("获取 ComfyUI")
    if not get_comfy(a.comfy_ref):
        return 1

    step("安装自定义节点")
    if not install_node():
        return 1

    step("安装依赖")
    if not install_deps(a.skip_deps, a.torch_index, a.install_torch):
        return 1

    step("模型")
    if a.download_models:
        if not download_models(["--tier", a.tier]):
            print("[提示] 有文件没下成功，可重跑 python download_models.py 断点续传")
    else:
        print_models()

    if a.link_models:
        step("联接模型目录")
        link_models()

    print("\n" + "=" * 84)
    print("安装完成。下一步：")
    print("  Windows：双击 MyPixelTool\\启动网页界面.bat")
    print("  其他平台：python MyPixelTool/webui.py")
    print("  浏览器打开 http://127.0.0.1:8899")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
