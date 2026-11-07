# -*- coding: utf-8 -*-
r"""把「自己的东西」打成一个可分享的 zip（不含模型、不含输出作品、不含 Python 环境）。

打包内容：
    MyPixelTool\        你的网页界面 + 流水线脚本（不含 输出\、__pycache__）
    ComfyUI\            ComfyUI 本体 + 自定义节点（不含 models\ 里的模型文件、output\temp\input\user）
    分享说明.txt        对方需要准备什么

产物：<根目录>\分享包\像素动画工坊-<日期>.zip
"""
import os
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "分享包")

# 相对 ROOT 的打包项：(路径, 需要跳过的子目录/文件名)
ITEMS = [
    ("MyPixelTool", {"输出", "__pycache__", "输出zip"}),
    ("ComfyUI", {"models", "output", "temp", "input", "user", "__pycache__",
                 "tests", ".git", "custom_nodes\\__pycache__"}),
    ("download_models.py", set()),      # 单文件：对方可以直接下载模型
    ("models", set()),                  # 只带 manifest.json（下面按扩展名跳过权重）
]

SKIP_EXT = {".pyc", ".pyo", ".log", ".safetensors", ".gguf", ".part", ".ckpt", ".bin"}

README = """像素动画工坊 · 分享包
=====================================================

这是一个基于 ComfyUI 的「像素游戏角色动画」生成工具：
文生图（Qwen-Image）→ 图生视频（MiniMax H3）→ 截帧 → 去背景（RMBG）→ 像素化

⚠ 本压缩包【不包含】两样东西（体积太大，需要自己准备）：
  1. Python 运行环境  → 目录 comfyui内置环境包\\（内含 python.exe、torch 等）
  2. 模型权重        → 目录 models\\

-----------------------------------------------------
对方拿到后怎么用
-----------------------------------------------------
1. 解压到任意目录，例如 D:\\像素动画工坊\\，得到：
       MyPixelTool\\        ← 工具本体
       ComfyUI\\            ← 推理后端
       comfyui内置环境包\\   ← 需要自己准备（见下）
       models\\             ← 需要自己准备（见下）

2. 准备运行环境（二选一）
   · 从作者处下载环境包 zip，解压后把 python.exe 所在目录命名为
     「comfyui内置环境包」放在根目录；或
   · 自己装 Python 3.11 + PyTorch(CUDA)，把该环境目录命名为 comfyui内置环境包。

3. 准备模型（本包已附带 download_models.py 与 models\\manifest.json）
   双击根目录「下载模型.bat」，它会问你要哪一档：
       [1] lite  约 15 GB   只出静态像素图（4~6GB 显存就够）
       [2] full  约 69 GB   出图 + 出动画（需要 8GB 显存）
       [3] auto  按显卡自动选
   也可以手动把权重按目录放进 models\\（diffusion_models / text_encoders / vae / background_removal）

4. 双击 MyPixelTool\\硬件检测.bat   ← 先看这台机器到底适合哪档，别下错

5. 双击 MyPixelTool\\初始化模型目录.bat
   （自动把 models\\ 里的目录「联接」进 ComfyUI\\models，不复制、不占双份空间）

6. 双击 MyPixelTool\\启动网页界面.bat
   浏览器打开 http://127.0.0.1:8899 即可使用。

   如果这台机器没有独显、或显卡带不动动画：
   在有显卡的电脑上双击 MyPixelTool\\启动服务端(局域网).bat，
   窗口会打印一行 http://192.168.x.x:8899 —— 把这行发给对方，
   对方用浏览器（手机/平板/核显本都行）打开就能用全部功能，什么都不用装。

-----------------------------------------------------
注意
-----------------------------------------------------
· 出动画建议 NVIDIA 8GB 显存以上 + 32GB 内存；驱动 cu130 需 580+
· 极简档（只出图）4~6GB 显存即可，但做不了动画
· 纯 CPU / 核显跑动画慢到不可用，这不是配置问题，是算力上限
· 首次启动 ComfyUI 需要 30~60 秒，状态栏变绿「ComfyUI 运行中」即可生成
· 生成结果默认只存在内存，点「接受并保存」才写入 MyPixelTool\\输出\\
"""


def should_skip(rel_dir, skip_names):
    parts = rel_dir.replace("/", "\\").split("\\")
    return any(p in skip_names for p in parts)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    out_path = os.path.join(OUT_DIR, f"像素动画工坊-{stamp}.zip")
    if os.path.exists(out_path):
        os.remove(out_path)

    total = 0
    file_count = 0
    t0 = time.time()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("分享说明.txt", README)

        for item, skip_names in ITEMS:
            src_root = os.path.join(ROOT, item)
            if os.path.isfile(src_root):
                zf.write(src_root, item)
                total += os.path.getsize(src_root)
                file_count += 1
                continue
            if not os.path.isdir(src_root):
                print(f"[警告] 缺少目录 {item}，已跳过")
                continue
            for dirpath, dirnames, filenames in os.walk(src_root):
                rel_dir = os.path.relpath(dirpath, ROOT)
                # 剪枝：跳过不需要的子目录
                dirnames[:] = [
                    d for d in dirnames
                    if d not in skip_names and not should_skip(os.path.join(rel_dir, d), skip_names)
                ]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                        continue
                    full = os.path.join(dirpath, fn)
                    arc = os.path.join(rel_dir, fn)
                    try:
                        size = os.path.getsize(full)
                        # 单个超过 50MB 的文件（基本是误放的模型）跳过
                        if size > 50 * 1024 * 1024:
                            print(f"[跳过] 大于50MB，疑似模型：{arc}")
                            continue
                        zf.write(full, arc)
                        total += size
                        file_count += 1
                    except OSError as e:
                        print(f"[跳过] {arc}: {e}")

    zsize = os.path.getsize(out_path)
    print("=" * 56)
    print(f"打包完成：{out_path}")
    print(f"  文件数 {file_count} 个 | 原始 {total/1024/1024:.1f} MB | 压缩后 {zsize/1024/1024:.1f} MB")
    print(f"  耗时 {time.time()-t0:.1f} 秒")
    print("  包内不含：models 模型、输出作品、Python 环境")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
