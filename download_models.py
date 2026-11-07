# -*- coding: utf-8 -*-
r"""下载 / 校验模型权重（不依赖任何第三方库）。

清单在 models/manifest.json：每个文件记录目标路径、字节数、SHA256、官方仓库。
默认从 hf-mirror.com（国内可直连）下载，失败自动退回 huggingface.co。

常用：
    python download_models.py --list          # 看清单和状态
    python download_models.py --check         # 只检查，不下载（缺什么一目了然）
    python download_models.py                 # 下载全部必需文件（约 68.7 GB）
    python download_models.py --only vae      # 只下名字里带 vae 的
    python download_models.py --sha           # 下完再做 SHA256 全量校验（较慢但最稳）
    python download_models.py --dest ..\ComfyUI\models   # 直接放进 ComfyUI

特点：
- 断点续传：中断后重跑会从 .part 文件续上，不会从头再来
- 多源回退：hf-mirror → huggingface，谁通用谁
- 大小校验：下完比对字节数，对不上会提示而不是假装成功
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "models", "manifest.json")
DEFAULT_DEST = os.path.join(HERE, "models")
GB = 1024 ** 3


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def load_manifest():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def urls_for(item, mirrors):
    """按优先级给出候选直链：自建镜像/网盘直链 → ModelScope → HF 镜像 → HF 官网。"""
    out = list(item.get("urls") or [])
    ms = item.get("modelscope") or {}
    if ms.get("repo") and ms.get("path"):
        out.append(f"https://modelscope.cn/models/{ms['repo']}/resolve/master/{ms['path']}")
    repo, path = item.get("repo"), item.get("path")
    if repo and path:
        for base in mirrors:
            out.append(f"{base.rstrip('/')}/{repo}/resolve/main/{path}")
    return out


def _req(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 PixelSpriteStudio")
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok.strip())
    return req


def probe_size(url, timeout=25):
    """用 Range 请求探活 + 拿总长度，避免把 20GB 的 404 页面下下来。"""
    req = _req(url)
    req.add_header("Range", "bytes=0-0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                return int(cr.rsplit("/", 1)[1])
            except ValueError:
                pass
        return int(r.headers.get("Content-Length") or 0)


def state_of(path, want):
    if not os.path.exists(path):
        return "missing", 0
    got = os.path.getsize(path)
    if got == want:
        return "ok", got
    # 官方文件与本地偶尔差几 KB（元数据重打包），1MB 内视为可用
    if abs(got - want) <= 1024 * 1024:
        return "close", got
    if got > want:
        return "toobig", got
    return "partial", got


def download_one(item, dest, mirrors, verify_sha=False, retries=3):
    target = os.path.join(dest, item["target"].replace("/", os.sep))
    want = int(item["size"])
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"

    st, got = state_of(target, want)
    if st in ("ok", "close"):
        if st == "close":
            print(f"  [可用] 已存在，大小差 {got - want:+d} 字节（元数据差异）：{item['target']}")
        return True
    if st == "toobig":
        print(f"  [重下] 现有文件比预期大，删除后重来：{item['target']}")
        os.remove(target)
        got = 0

    cands = urls_for(item, mirrors)
    if not cands:
        print(f"  [跳过] 清单里没有可下载来源：{item['target']}")
        return False

    last_err = ""
    for url in cands:
        host = url.split("/")[2]
        try:
            total = probe_size(url)
        except urllib.error.HTTPError as e:
            last_err = f"{host}: HTTP {e.code}"
            if e.code in (401, 403):
                print(f"  [需要授权] {host} 拒绝访问（403/401）。"
                      f"若是受限模型，请先在 huggingface.co 同意许可并设置环境变量 HF_TOKEN")
            else:
                print(f"  [探测失败] {host} — HTTP {e.code}")
            continue
        except Exception as e:
            last_err = f"{host}: {e}"
            print(f"  [探测失败] {host} — {e}")
            continue
        if total and abs(total - want) > 1024 * 1024:
            print(f"  [跳过源] {host} 的文件大小 {human(total)} 与清单 {human(want)} 不符")
            continue

        for attempt in range(1, retries + 1):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have > want:
                os.remove(part)
                have = 0
            req = _req(url)
            if have:
                req.add_header("Range", f"bytes={have}-")
            try:
                t0, last = time.time(), 0.0
                with urllib.request.urlopen(req, timeout=60) as r:
                    mode = "ab" if (have and r.status == 206) else "wb"
                    if mode == "wb":
                        have = 0
                    print(f"  源 {host} · 从 {human(have)} 继续 · 共 {human(want)}")
                    with open(part, mode) as f:
                        while True:
                            chunk = r.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            have += len(chunk)
                            now = time.time()
                            if now - last >= 0.4 or have == want:
                                last = now
                                sp = have / max(now - t0, 0.001)
                                pct = 100.0 * have / want if want else 0
                                eta = (want - have) / sp if sp > 1 else 0
                                print(f"\r    {pct:5.1f}%  {human(have)} / {human(want)}"
                                      f"  {human(sp)}/s  剩余 {int(eta)//60}分{int(eta)%60:02d}秒   ",
                                      end="", flush=True)
                    print()
                if os.path.getsize(part) == want:
                    break
                last_err = f"下载不完整（{human(os.path.getsize(part))} / {human(want)}）"
                print(f"  [重试 {attempt}/{retries}] {last_err}")
            except Exception as e:
                last_err = f"{host}: {e}"
                print(f"  [重试 {attempt}/{retries}] {last_err}")
                time.sleep(2 * attempt)
        else:
            continue
        if os.path.exists(part) and os.path.getsize(part) == want:
            os.replace(part, target)
            print(f"  √ 完成 {item['target']}")
            if verify_sha and item.get("sha256"):
                ok = check_sha(target, item["sha256"])
                print("    SHA256 " + ("一致" if ok else "不一致！文件可能损坏，请删除后重下"))
                if not ok:
                    return False
            return True
    print(f"  × 失败 {item['target']} —— {last_err}")
    return False


def check_sha(path, expect):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest().lower() == (expect or "").lower()


def print_table(items, dest, all_required=False):
    print(f"\n{'':<3}{'文件':<68}{'大小':>9}  状态")
    print("-" * 94)
    missing = 0
    need = 0
    for it in items:
        p = os.path.join(dest, it["target"].replace("/", os.sep))
        st, got = state_of(p, int(it["size"]))
        tag = {"ok": "已就绪", "close": "已就绪(大小略有差异)", "missing": "缺失",
               "partial": f"不完整 {human(got)}", "toobig": "大小异常"}[st]
        req = all_required or it.get("required", True)
        if req:
            need += int(it["size"])
            if st not in ("ok", "close"):
                missing += 1
        print(f"{'*' if req else ' '}  {it['target']:<68}{human(it['size']):>9}  {tag}")
    print("-" * 94)
    print(f"必需文件合计 {human(need)}，当前缺 {missing} 个\n")
    return missing


def _vram_gb():
    """粗查显存，供 --tier auto 判断档位"""
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, timeout=12).stdout.decode("utf-8", "ignore")
        first = [x for x in out.strip().splitlines() if x.strip()]
        if first:
            return round(float(first[0].strip()) / 1024, 1)
    except Exception:
        pass
    return 0


def pick_tier(man, want):
    """返回 (档位名, 文件列表)。auto 时按显存判断"""
    tiers = man.get("tiers") or {}
    if not tiers:
        return "full", [i for i in man.get("files", []) if i.get("required", True)]
    if want == "auto":
        vram = _vram_gb()
        want = "full" if vram >= 8 else "lite"
        print(f"[自动] 检测到显存 {vram}GB -> 选择「{tiers.get(want, {}).get('label', want)}」")
    conf = tiers.get(want)
    if not conf:
        print(f"[错误] 清单里没有档位 {want}")
        return want, []
    targets = set(conf.get("targets") or [])
    items = [i for i in man.get("files", []) if i["target"] in targets]
    return want, items


def main():
    ap = argparse.ArgumentParser(description="下载/校验 Pixel Sprite Studio 的模型权重")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="模型根目录（默认仓库内 models/）")
    ap.add_argument("--check", action="store_true", help="只检查，不下载")
    ap.add_argument("--list", action="store_true", help="列出清单与状态后退出")
    ap.add_argument("--only", action="append", default=[], help="只处理包含该关键字的文件（可多次）")
    ap.add_argument("--all", action="store_true", help="连可选文件（音频 VAE）一起下")
    ap.add_argument("--tier", choices=["auto", "lite", "full"], default="auto",
                    help="lite=只出图约15GB；full=含动画约69GB；auto=按显存自动选（默认）")
    ap.add_argument("--sha", action="store_true", help="下载后做 SHA256 校验")
    ap.add_argument("--mirror", choices=["auto", "hf-mirror", "huggingface"],
                    default="auto", help="下载源，默认 auto（镜像优先，失败退回官网）")
    a = ap.parse_args()

    if not os.path.isfile(MANIFEST):
        print(f"[错误] 找不到清单 {MANIFEST}")
        return 1
    man = load_manifest()
    m = man.get("mirrors", {})
    if a.mirror == "auto":
        mirrors = [m.get("hf-mirror", "https://hf-mirror.com"), m.get("huggingface", "https://huggingface.co")]
    elif a.mirror == "hf-mirror":
        mirrors = [m.get("hf-mirror", "https://hf-mirror.com")]
    else:
        mirrors = [m.get("huggingface", "https://huggingface.co")]

    tier, items_required = pick_tier(man, a.tier)
    conf = (man.get("tiers") or {}).get(tier) or {}
    if conf:
        print(f"档位：{conf.get('label', tier)}（{human(conf.get('bytes', 0))}）")
        if conf.get("note"):
            print(f"      {conf['note']}")
    if a.all:
        extra = [i for i in man.get("files", []) if not i.get("required", True)]
        have = {i["target"] for i in items_required}
        items_required = items_required + [i for i in extra if i["target"] not in have]
    if a.only:
        items_required = [i for i in items_required if any(k.lower() in i["target"].lower() for k in a.only)]

    dest = os.path.abspath(a.dest)
    print(f"模型目录：{dest}")
    print(f"下载源：{', '.join(mirrors)}")

    missing = print_table(items_required, dest, all_required=not a.all)
    if a.check or a.list:
        return 1 if missing else 0
    if not items_required:
        print("没有匹配的文件")
        return 0

    free = shutil.disk_usage(dest if os.path.isdir(dest) else HERE).free
    todo = sum(int(i["size"]) for i in items_required
               if state_of(os.path.join(dest, i["target"].replace("/", os.sep)), int(i["size"]))[0]
               not in ("ok", "close"))
    if todo and free < todo * 1.05:
        print(f"[警告] 剩余空间 {human(free)}，需要约 {human(todo)}，可能不够")

    ok = True
    for i, it in enumerate(items_required, 1):
        print(f"\n[{i}/{len(items_required)}] {it['target']}")
        print(f"      {it.get('label','')}  ({it.get('license','')})")
        if not download_one(it, dest, mirrors, verify_sha=a.sha):
            ok = False

    print_table(items_required, dest, all_required=not a.all)
    if ok:
        print("全部就绪。下一步：python MyPixelTool/setup_models.py  （把 models/ 联接进 ComfyUI）")
    else:
        print("有文件没下成功。可以重跑本脚本（会断点续传）；或按 README 的备用方式手动下载。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
