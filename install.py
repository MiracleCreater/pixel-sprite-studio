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
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_DIR = os.path.join(HERE, "ComfyUI")
NODE_SRC = os.path.join(HERE, "custom_nodes", "pixel_memory")
NODE_DST = os.path.join(COMFY_DIR, "custom_nodes", "pixel_memory")
COMFY_REPO = "https://github.com/comfyanonymous/ComfyUI.git"

DEFAULT_REF = "v0.42.0"          # 本工具适配过的版本
TORCH_CU130 = "https://download.pytorch.org/whl/cu130"

MODELS = [
    ("diffusion_models", "qwen_image_2.1_turbo_bf16.safetensors", "13.3 GB", "文生图主模型（Qwen-Image 2.1 Turbo）"),
    ("diffusion_models", "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors", "20.6 GB", "图生视频主模型（MiniMax H3 8步加速）"),
    ("text_encoders", "qwen3vl_8b_fp8_scaled.safetensors", "9.9 GB", "文生图文本编码器"),
    ("text_encoders", "qwen3vl_32b_minimax_h3_int4_convrot.safetensors", "13.9 GB", "视频文本编码器"),
    ("vae", "qwen_image_2.1_vae_bf16.safetensors", "0.6 GB", "图像 VAE"),
    ("vae", "minimax_h3_video_vae_fp16.safetensors", "4.9 GB", "视频 VAE"),
    ("background_removal", "rmbg2.0.safetensors", "0.8 GB", "抠图模型（RMBG 2.0）"),
]


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


def print_models():
    print("\n模型清单（放到 ComfyUI/models/<目录>/ 下，或放在本仓库 models/<目录>/ 后执行 --link-models）")
    print("-" * 78)
    for folder, name, size, note in MODELS:
        print(f"  {folder + '/' + name:<62} {size:>7}  {note}")
    print("-" * 78)
    print(f"  合计约 64 GB；缺失任意一个都会在生成时报错")
    print("  国内可从 ModelScope / HuggingFace 镜像下载，文件名必须保持一致")


def main():
    ap = argparse.ArgumentParser(description="安装 Pixel Sprite Studio")
    ap.add_argument("--comfy-ref", default=DEFAULT_REF, help="ComfyUI 分支或标签（默认 %s）" % DEFAULT_REF)
    ap.add_argument("--skip-deps", action="store_true", help="跳过 pip 依赖安装")
    ap.add_argument("--install-torch", action="store_true", help="安装 CUDA 版 PyTorch")
    ap.add_argument("--torch-index", default=None, help="PyTorch 源，如 cu128/cu130")
    ap.add_argument("--link-models", action="store_true", help="把 models/ 联接进 ComfyUI/models")
    a = ap.parse_args()

    print("=" * 78)
    print("  Pixel Sprite Studio · 安装")
    print("=" * 78)

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

    if a.link_models:
        step("联接模型目录")
        link_models()

    step("模型清单")
    print_models()

    print("\n" + "=" * 78)
    print("安装完成。下一步：")
    print("  Windows：双击 MyPixelTool\\启动网页界面.bat")
    print("  其他平台：python MyPixelTool/webui.py")
    print("  浏览器打开 http://127.0.0.1:8899")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
