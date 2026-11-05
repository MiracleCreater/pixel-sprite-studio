# Pixel Sprite Studio · 像素游戏角色动画生成器

本地跑的一套**像素风游戏精灵（sprite）动画生产线**：给一句角色描述，自动产出**透明背景的像素动画帧序列**，可直接导入 Aseprite / Unity / Godot。

全程本地推理，不需要任何在线服务、不需要激活码。

```
文生图（Qwen-Image 2.1 Turbo）
      ↓  一次生成 2/4/6/8 张候选，你挑一张
图生视频（MiniMax H3 · 8 步加速）
      ↓
截帧 → 去背景（RMBG 2.0）→ 像素化（调色板量化 + 最近邻放大）
      ↓
你确认后才写硬盘
```

## 特性

- **先挑图再生动作**：批量出候选角色图，选中满意的再进动画环节，不浪费算力
- **零落盘试错**：所有中间结果（候选图、视频帧、遮罩）都存在 ComfyUI 进程内存里，只有点「接受」才写文件——反复试错不磨损硬盘
- **8GB 显存可跑**：视频模型与抠图模型分时占用显存，去背景分块处理 + 显存不足自动减半重试
- **网页界面**：资产库 / 批量出图 / 动作多选 / 待确认预览 / 透明背景动画播放器 / PNG 序列 ZIP 导出
- **命令行可用**：`pipeline.py` 支持脚本化批量生成

## 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10/11（脚本为 .bat；其他平台可用 Python 直接启动） |
| 显卡 | NVIDIA，**8GB 显存以上**（本机实测 RTX 5060 Laptop 8GB） |
| 驱动 | 支持 **CUDA 12.8+**（cu130 环境需 580 以上驱动） |
| Python | 3.10 ~ 3.12 |
| 磁盘 | 约 80 GB（模型 64 GB + 依赖 ~8 GB） |
| 其他 | Git；能访问 GitHub / PyPI（或配置镜像） |

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/<你的用户名>/pixel-sprite-studio.git
cd pixel-sprite-studio

# 2. 安装（拉取 ComfyUI + 装内存节点 + 装依赖）
python install.py

# 若还没装 PyTorch（CUDA 版），加上这个参数一次装好：
python install.py --install-torch
```

**3. 放模型**（见下方清单）——可放在 `ComfyUI/models/<目录>/`，或放在仓库 `models/<目录>/` 后执行：

```bash
python install.py --link-models
```

**4. 启动**

```
Windows：双击 MyPixelTool\启动网页界面.bat
其他：  python MyPixelTool/webui.py
```

浏览器会自动打开 <http://127.0.0.1:8899>。首次启动 ComfyUI 需要 30~60 秒，状态栏变绿即可生成。

## 模型清单

共 7 个文件、约 64 GB。**文件名必须完全一致**（程序按名字加载）。

| 放置目录 | 文件名 | 大小 |
|---|---|---|
| `diffusion_models/` | `qwen_image_2.1_turbo_bf16.safetensors` | 13.3 GB |
| `diffusion_models/` | `fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors` | 20.6 GB |
| `text_encoders/` | `qwen3vl_8b_fp8_scaled.safetensors` | 9.9 GB |
| `text_encoders/` | `qwen3vl_32b_minimax_h3_int4_convrot.safetensors` | 13.9 GB |
| `vae/` | `qwen_image_2.1_vae_bf16.safetensors` | 0.6 GB |
| `vae/` | `minimax_h3_video_vae_fp16.safetensors` | 4.9 GB |
| `background_removal/` | `rmbg2.0.safetensors` | 0.8 GB |

> 模型权重版权归各自作者所有，本项目不附带、不转售。请从官方（HuggingFace / ModelScope / 作者发布页）下载。

## 使用流程

1. **① 角色**：填资产名 + 角色提示词 → 选候选数量 → 「批量出候选图」→ 点选满意的一张（也可直接拖图）
2. **② 动作**：勾选动作（站立/行走/攻击/受伤，或自定义）→ 调时长与像素颗粒 → 「生成动作」
3. **③ 待确认**：结果先进「待你确认」区（带动画预览），满意点「接受并保存」，否则「丢弃」释放内存
4. **④ 已保存**：资产库里可播放、逐帧查看、导出 PNG 序列 ZIP、追加新动作

## 命令行

```bash
# 生成角色 + 多个动作，直接保存
python MyPixelTool/pipeline.py --name 小骑士 --prompt "像素风格小骑士，蓝色斗篷" --actions 站立 攻击

# 用现成图片当角色图
python MyPixelTool/pipeline.py --name 小骑士 --image D:\角色.png --actions 行走

# 参数：--duration 秒数  --pixel 32~128  --steps 4~8  --video-size 640x352  --keep-video
```

## 目录结构

```
pixel-sprite-studio/
├── install.py                    安装脚本
├── MyPixelTool/
│   ├── pipeline.py               流水线（工作流构建 / 内存中转 / 像素化）
│   ├── webui.py                  网页后端（标准库 HTTP，无额外框架）
│   ├── web/index.html            前端（单文件，零依赖）
│   ├── setup_models.py           把 models/ 联接进 ComfyUI/models
│   └── 启动网页界面.bat          日常启动
├── custom_nodes/pixel_memory/    自定义节点：结果只存内存，不落盘
├── models/                       模型放这里（或直接放 ComfyUI/models）
└── ComfyUI/                      安装脚本自动克隆（不入库）
```

## 常见问题

**去背景报 out of memory？**
已内置防护：去背景前自动卸载视频模型、按 8 帧分块、显存不足自动减半重试。若仍失败，关掉占用显存的程序（游戏、开了硬件加速的浏览器）再试。

**提示「缺少内存暂存节点」**
ComfyUI 是旧进程，关掉 ComfyUI 窗口后重新生成即可（会自动重启并加载新节点）。

**端口 8899 / 8188 被占用**
8899 是网页界面，重复启动只会再开一个浏览器页；8188 是 ComfyUI，若被别的 ComfyUI 占用，请先关掉它。

**能不用网页界面吗？**
可以，`pipeline.py` 完全独立，命令行直接跑，生成后落盘。

**支持 Linux / macOS？**
核心逻辑跨平台，但 `.bat` 启动脚本和目录联接是 Windows 方案。其他平台用 `python MyPixelTool/webui.py` 启动，模型直接放进 `ComfyUI/models/` 即可。

## 许可证

- 本项目代码：**MIT**（见 `LICENSE`）
- `custom_nodes/pixel_memory`：作为 ComfyUI 插件运行，遵循 **GPL-3.0**
- ComfyUI 由安装脚本单独拉取，遵循其自身 **GPL-3.0**，不包含在本仓库中

## 致谢

[ComfyUI](https://github.com/comfyanonymous/ComfyUI) · Qwen-Image · MiniMax H3 · [RMBG 2.0](https://github.com/Bria-AI/RMBG-2.0)
