# -*- coding: utf-8 -*-
"""把 <工具根目录>\models 下的模型目录，联接进 ComfyUI\models（目录联接，不复制、不占双份空间）。

换电脑 / 别人拿到分享包时，只要把模型放在根目录 models\ 下，双击「初始化模型目录.bat」即可。

用法：
    python setup_models.py            交互确认后执行
    python setup_models.py --yes      不询问直接执行
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "models")
DST = os.path.join(ROOT, "ComfyUI", "models")

FOLDERS = ["diffusion_models", "text_encoders", "vae", "loras", "background_removal"]


def _is_junction(path):
    try:
        return bool(os.readlink(path))
    except (OSError, ValueError, AttributeError):
        return False


def _remove_link(path):
    """只删除目录联接本身，绝不动目标目录内容。"""
    subprocess.run(["cmd", "/c", "rmdir", path], capture_output=True, text=True)


def main():
    if not os.path.isdir(SRC):
        print(f"[错误] 找不到模型目录：{SRC}")
        print("       请在该位置建一个 models 文件夹，把模型按子目录放进去。")
        return 1
    if not os.path.isdir(DST):
        print(f"[错误] 找不到 ComfyUI 模型目录：{DST}")
        print("       确认 ComfyUI 目录是否完整。")
        return 1

    plan = []
    for name in FOLDERS:
        s = os.path.join(SRC, name)
        d = os.path.join(DST, name)
        if not os.path.isdir(s):
            print(f"[跳过] 源目录不存在：models\\{name}")
            continue
        if os.path.isdir(d):
            if _is_junction(d):
                print(f"[已就绪] ComfyUI\\models\\{name} 已是联接")
                continue
            entries = os.listdir(d)
            real = [e for e in entries if not e.startswith("put_")]
            if real:
                print(f"[跳过] ComfyUI\\models\\{name} 是真实目录且非空，不覆盖")
                continue
            plan.append((name, s, d, True))   # True = 先删空占位目录
        else:
            plan.append((name, s, d, False))

    if not plan:
        print("\n没有需要处理的项目。")
        return 0

    print("\n将要创建以下目录联接：")
    for name, s, d, need_del in plan:
        print(f"  ComfyUI\\models\\{name}  ->  models\\{name}")

    if "--yes" not in sys.argv:
        try:
            if input("\n继续？(Y/n) ").strip().lower() not in ("", "y", "yes"):
                print("已取消。")
                return 0
        except EOFError:
            pass

    ok = 0
    for name, s, d, need_del in plan:
        try:
            if need_del:
                os.rmdir(d)
            done = subprocess.run(
                ["cmd", "/c", "mklink", "/J", d, s],
                capture_output=True, text=True,
            )
            if done.returncode == 0:
                print(f"[完成] {name}")
                ok += 1
            else:
                print(f"[失败] {name}: {(done.stderr or done.stdout).strip()}")
        except Exception as e:
            print(f"[失败] {name}: {e}")

    print(f"\n共创建 {ok} 个联接。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
