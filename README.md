# Pixel Sprite Studio · 像素游戏角色动画生成器

给一句角色描述，本地产出**透明背景的像素动画帧序列**，可直接导入 Aseprite / Unity / Godot。
全程本地推理，不需要任何在线服务、不需要激活码。

![demo](docs/demo-attack.gif)

```
文生图（Qwen-Image 2.1 Turbo）
      ↓  一次生成 2/4/6/8 张候选，你挑一张
图生视频（MiniMax H3 · 8 步加速）
      ↓
截帧 → 去背景（RMBG 2.0）→ 像素化（调色板量化 + 最近邻放大）
      ↓
你确认后才写硬盘
```

![界面](docs/ui-overview.png)

## 特性

- **先挑图再生动作**：批量出候选角色图，选中满意的再进动画环节，不浪费算力
- **零落盘试错**：所有中间结果（候选图、视频帧、遮罩）都存在 ComfyUI 进程内存里，只有点「接受」才写文件
- **8GB 显存可跑，4GB 能出图**：视频模型与抠图模型分时占用显存，去背景分块处理 + 显存不足自动减半重试；模型分「极简 / 全量」两档，装哪档用哪档
- **低配不用干瞪眼**：内置硬件体检（直接告诉你这台机器能跑什么）+ 局域网共享（一台显卡机当服务端，核显本、平板、手机开浏览器就能用）
- **网页界面**：资产库 / 批量出图 / 动作多选（支持自定义动作）/ 待确认预览 / 透明背景播放器 / PNG 序列导出
- **资产管理**：点一下已保存的资产即可载入它的角色图继续生成新动作；资产和动作都可以随时删除
- **命令行可用**：`pipeline.py` 支持脚本化批量生成

## 环境要求

**先看这张表，对号入座，不用猜：**

| 你的电脑 | 能做什么 | 模型下载量 |
|---|---|---|
| 没有独显（核显本 / 轻薄本） | 本机只能很慢地出静态图；**推荐当"浏览器客户端"连一台有显卡的机器**，功能一个不少 | 0 |
| 4~6 GB 显存（GTX 1650 / 3050 6G / 2060） | 极简档：出静态像素图，做不了动画 | 约 **15 GB** |
| 8 GB 显存 + 32 GB 内存（RTX 3060 / 4060 / 5060） | 全量档：出图 + 出动画 | 约 **69 GB** |
| 12 GB 以上显存 | 全量档，可上更高分辨率、更长的动画 | 约 **69 GB** |

不确定自己属于哪档？双击 **`硬件检测.bat`**，它会直接告诉你"这台机器能跑什么、还差什么"。

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10/11（脚本为 .bat；其他平台用 Python 直接启动） |
| 显卡 | 出动画：NVIDIA ≥ 8 GB 显存；只出图：≥ 4 GB 显存，或纯 CPU（很慢） |
| 驱动 | cu130 环境需 580 以上；老驱动可用 `--torch-index cu128` 安装 |
| Python | 3.10 ~ 3.12 |
| 磁盘 | 极简档约 25 GB；全量档约 80 GB |
| 其他 | Git；能访问 GitHub / PyPI / hf-mirror（国内均可直连） |

> 为什么不能把动画硬塞进低配机器：视频模型是 200 亿参数的扩散模型，纯 CPU / 核显推理一个 2 秒动作要几十分钟到几小时，社区的一致结论是"基本不可用"。所以低配机器我们不硬做，而是让你**连一台有显卡的机器**（见路线 D），效果和在本地跑完全一样。

## 快速开始

### 路线 A · 一键安装（推荐）

```bash
git clone https://github.com/MiracleCreater/pixel-sprite-studio.git
cd pixel-sprite-studio
```

Windows 直接双击 **`一键安装.bat`**，它会依次完成：

1. **硬件体检**：告诉你这台机器适合哪一档
2. 克隆 ComfyUI v0.42.0 + 安装内存暂存节点
3. 安装 Python 依赖（CUDA 版 PyTorch 约 3GB + ComfyUI requirements）
4. **按档位下载模型**（极简档 15 GB / 全量档 69 GB，断点续传，国内默认走 hf-mirror / ModelScope）
5. 把模型目录联接进 ComfyUI（不占双份空间）

完成后双击 **`Start-WebUI.bat`**（或 `MyPixelTool\启动网页界面.bat`），浏览器打开 <http://127.0.0.1:8899>。

> 命令行等价写法：`python install.py --install-torch --download-models --link-models --tier auto`
> 已装过依赖的可以拆开跑：`python install.py --skip-deps` → `下载模型.bat` → `python install.py --link-models`
> 只想先出图：双击 `下载模型.bat` 选 `[1] lite`；以后想加动画再选 `[2] full`（已下好的文件不会重复下载）

### 路线 B · 懒人整合包（不想装 Python）

从网盘（链接见下方「离线包」）下载两个文件，解压到同一目录：

| 文件 | 大小 | 内容 |
|---|---|---|
| `像素动画工坊-整合包.zip` | ~20 MB | 工具 + ComfyUI + 内存节点 |
| `comfyui内置环境包.7z` | ~5.2 GB | 免安装 Python 3.11 + PyTorch cu130 + 全部依赖 |

然后：`初始化模型目录.bat` → `下载模型.bat`（或手动放模型）→ `启动网页界面.bat`。

### 路线 C · 我已经有模型

把权重按下面的目录名放进 `models/`（或直接放进 `ComfyUI/models/`），然后：

```bash
python install.py --link-models     # 建联接，不复制文件
python download_models.py --check --tier full   # 校验文件名和大小
```

### 路线 D · 局域网共享（低配机器 / 团队首选）

**一台有显卡的电脑当服务端，其他设备只开浏览器。** 没有独显的笔记本、平板、手机都能用，功能完全一样，什么都不用装。

1. 在显卡机上装好本工具，双击 **`MyPixelTool\启动服务端(局域网).bat`**
2. 窗口里会打印一行 `其他设备  http://192.168.x.x:8899`
3. 把那行地址发给别人，用浏览器打开即可
4. 首次启动 Windows 可能弹防火墙提示，点**允许访问**，否则别人连不上

适合：给同事 / 学生 / 玩家试用；家里一台游戏本当主机，其他设备都来用；工作室多人排队出图。

> 注意：同一局域网内有权访问的人也能看到和删除输出目录里的资产，别放在不可信的网络里。

## 模型清单

两档可选，**程序会自动用你实际装了的文件**，装了哪档就解锁哪档功能；文件名必须完全一致。

| 档位 | 包含 | 体积 | 能做什么 |
|---|---|---|---|
| **极简档 lite** | 出图 3 件 + 抠图 1 件 | **15.1 GB** | 生成角色图 → 像素化 → 存 PNG（不做动画） |
| **全量档 full** | 极简档的 4 件 + 动画 3 件 | **68.7 GB** | 全部功能：候选图、动作动画、挂机批量 |

清单的机器可读版在 [`models/manifest.json`](models/manifest.json)（含精确字节数、SHA256、档位归属）。

**极简档（4 个文件，15.1 GB）**

| 放置目录 | 文件 | 大小 | 来源 |
|---|---|---|---|
| `diffusion_models/` | `qwen_image_2.1_turbo_int8_convrot.safetensors` | 7.3 GB | [Comfy-Org/Qwen-Image-2.1](https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1) |
| `text_encoders/` | `qwen3vl_8b_w4a8.safetensors` | 6.3 GB | [Comfy-Org/Qwen-Image-2.1](https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1) |
| `vae/` | `qwen_image_2.1_vae_bf16.safetensors` | 0.6 GB | [Comfy-Org/Qwen-Image-2.1](https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1) |
| `background_removal/` | `rmbg2.0.safetensors` | 0.8 GB | [briaai/RMBG-2.0](https://hf-mirror.com/briaai/RMBG-2.0) ²（下载后自动改名） |

**全量档额外需要的动画文件（53.6 GB）**

| 放置目录 | 文件 | 大小 | 来源 |
|---|---|---|---|
| `diffusion_models/` | `qwen_image_2.1_turbo_bf16.safetensors` | 14.2 GB | [Comfy-Org/Qwen-Image-2.1](https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1) |
| `diffusion_models/` | `fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors` | 22.1 GB | [FastVideo/FastVideo-FastH3-Comfy](https://hf-mirror.com/FastVideo/FastVideo-FastH3-Comfy) |
| `text_encoders/` | `qwen3vl_32b_minimax_h3_int4_convrot.safetensors` | 15.0 GB | [Merserk/MiniMax-H3-INT4-ConvRot](https://hf-mirror.com/Merserk/MiniMax-H3-INT4-ConvRot) ¹ |
| `text_encoders/` | `qwen3vl_8b_fp8_scaled.safetensors` | 10.6 GB | [Comfy-Org/Qwen3-VL](https://hf-mirror.com/Comfy-Org/Qwen3-VL) |
| `vae/` | `minimax_h3_video_vae_fp16.safetensors` | 5.2 GB | [Comfy-Org/MiniMax-H3](https://hf-mirror.com/Comfy-Org/MiniMax-H3) |

> 出图模型两个版本都在时优先用 bf16（画质最好）；只有 int8 版也能跑，省 7 GB 显存。
> 可选：`vae/minimax_h3_audio_vae_fp32.safetensors`（0.6 GB）——本工具不生成音频，用不到。

> ¹ 官方仓库只有 bf16/int8/nvfp4 版；8GB 显存跑视频编码器需要这个 int4 量化版（清单里已记 SHA256）。
> ² briaai 官方仓库需要登录同意许可；脚本已配置 ModelScope 镜像直链，通常无需 token。
> 上面链接是 hf-mirror（国内直连），把域名换成 `huggingface.co` 即为官方地址。

## 使用流程

1. **① 角色**：填资产名 + 角色提示词 → 选候选数量 → 「批量出候选图」→ 点选满意的一张（也可直接拖图）
2. **② 动作**：勾选动作（站立/行走/攻击/受伤，或「＋ 自定义动作」）→ 调时长与像素颗粒 → 「生成动作」
3. **③ 待确认**：结果先进「待你确认」区（带动画预览），满意点「接受并保存」，否则「丢弃」释放内存
4. **④ 已保存**：播放 / 逐帧查看 / 导出 PNG 序列 ZIP；点左侧任意资产可载入其角色图**继续生成新动作**

![播放器](docs/ui-player.png)

### 挂机模式（批量无人值守）

第 ⑤ 张卡片：列一个「资产名称 + 提示词」的队列，点 **「▶ 执行队列」** 后全程自动——
每个资产出 1 张图（无脑取第一张）当角色图，依次跑完第 ② 步勾选的动作序列，
**每个动作生成完立刻存盘**，不等确认。n 个资产 × m 个动作 = n×m 个输出。

- **输出目录可换盘**：点「浏览…」弹系统文件夹选择框，任意盘 / 新建文件夹都行；不可写会当场报错
- 队列支持**批量粘贴**导入：`【资产名称：骑士A。提示词：像素风骑士，蓝披风。】`、`名称：X 提示词：Y`、`X | Y` 混用都行
- 单个资产失败自动跳过并记入日志，不会中断整个队列；「■ 停止」在当前动作完成后生效
- 同名资产会覆盖同名动作目录，想保留旧结果就换个名字


## 命令行

```bash
# 生成角色 + 多个动作，直接保存
python MyPixelTool/pipeline.py --name 小骑士 --prompt "像素风格小骑士，蓝色斗篷" --actions 站立 攻击

# 用现成图片当角色图
python MyPixelTool/pipeline.py --name 小骑士 --image D:\角色.png --actions 行走

# 参数：--duration 秒数  --pixel 32~128  --steps 4~8  --video-size 640x352  --keep-video
```

## 离线包（网盘备用 · 不装 Python 也能跑）

> GitHub 路线需要本机有 Python 3.10+。如果连 Python 都不想装、或者 `pip install torch`
> 一直失败，走这条路线：**解压即用，整个工具自带解释器**。

**下载**：夸克网盘 <https://pan.quark.cn/s/1204f8f53360>（目录 `pixel-sprite-studio/`）

| 文件 | 大小 | 说明 |
|---|---|---|
| `像素动画工坊-整合包.zip` | ~20 MB | 工具本体 + ComfyUI 0.42.0 + 内存节点（不含模型） |
| `comfyui内置环境包.zip` | 3.1 GB（解压 5 GB） | 便携 Python 3.11 + PyTorch 2.10 cu130，已在目标机器验证可跑 |

**用法**（两个 zip 都下下来，解压到同一个目录）：

```
任意文件夹\
├── MyPixelTool\            ← 来自「整合包」
├── ComfyUI\                ← 来自「整合包」
└── comfyui内置环境包\       ← 来自「环境包」（python.exe 在这个目录里）
```

1. 双击 `MyPixelTool\初始化模型目录.bat` —— 把 `..\models\` 联接进 `ComfyUI\models\`
2. 把模型放进 `models\<子目录>\`（照 `models\manifest.json` 的 `target` 字段摆；见下方「模型」）
3. 双击 `MyPixelTool\启动网页界面.bat` —— 它会自动找到旁边的 `comfyui内置环境包\python.exe`
4. 浏览器打开 <http://127.0.0.1:8899>

> 环境包里的 torch 是 `2.10.0+cu130`，需要 **NVIDIA 驱动 >= 580**（含 CUDA 12.8 以上）。
> 驱动太老会报 `CUDA driver version is insufficient`，去 NVIDIA 官网更新即可，与网盘包无关。
>
> 网盘链接若失效请到 [Issues](https://github.com/MiracleCreater/pixel-sprite-studio/issues) 反馈。
> 模型本身**不必**从网盘下 —— `download_models.py` 能自动下全并校验。

## 目录结构

```
pixel-sprite-studio/
├── install.py                    安装脚本（clone ComfyUI / 装节点 / 装依赖 / 联接模型）
├── download_models.py            模型下载与校验（断点续传，按 lite / full 档位取清单）
├── 一键安装.bat / 下载模型.bat / Start-WebUI.bat
├── MyPixelTool/
│   ├── pipeline.py               流水线（工作流构建 / 内存中转 / 像素化 / 按实际模型自动选档）
│   ├── webui.py                  网页后端（标准库 HTTP，无额外框架；支持 --lan 局域网共享）
│   ├── check_env.py              硬件体检：显卡/显存/内存/磁盘/模型齐全度 → 给出档位建议
│   ├── web/index.html            前端（单文件，零依赖）
│   ├── 硬件检测.bat              双击跑体检
│   ├── 启动网页界面.bat          本机使用（127.0.0.1）
│   ├── 启动服务端(局域网).bat    局域网共享（0.0.0.0，打印其他设备可用地址）
│   └── setup_models.py           把 models/ 联接进 ComfyUI/models
├── custom_nodes/pixel_memory/    自定义节点：结果只存内存，不落盘
├── models/                       模型放这里（manifest.json 是权威清单，含 tiers 档位定义）
└── ComfyUI/                      安装脚本自动克隆（不入库）
```

## 常见问题

**我这台是核显本 / 没有独显，能用吗？**
能用，但别指望它自己算。双击 `硬件检测.bat`，它会告诉你结论。可行做法是走**路线 D**：找一台有显卡的电脑跑服务端，你这台只开浏览器，功能一个不少。硬要在核显上本地跑，静态图一张要几分钟到十几分钟，动画基本不可用——这不是配置问题，是算力物理上限。

**显存 6 GB，能出动画吗？**
能跑但慢（比 8 GB 大约慢一半），建议：装全量档后只放出图 int8 模型（省 7 GB 显存）、视频时长压到 2 秒、分辨率用 640×352。程序会自动优先用能找到的最省显存组合。

**怎么从极简档升级到全量档？**
双击 `下载模型.bat` 选 `[2] full`，只会补下缺的 53.6 GB，已有文件跳过。下完关掉服务重新启动即可，界面顶部的档位标签会变成「全量档」。

**模型下载 403 / 需要授权？**
`rmbg2.0` 已配置 ModelScope 直链，一般不会遇到。若某个源要求登录：到 huggingface.co 同意该模型许可，然后 `set HF_TOKEN=你的token` 再重跑下载脚本。

**下载中断了怎么办？**
直接重跑 `下载模型.bat`，已完成的文件会跳过，未完成的从断点继续。

**去背景报 out of memory？**
已内置防护：去背景前自动卸载视频模型、按 8 帧分块、显存不足自动减半重试。若仍失败，关掉占用显存的程序再试。

**提示「缺少内存暂存节点」**
ComfyUI 是旧进程，关掉后重新生成即可（会自动重启并加载新节点）。

**局域网里别人打不开我的地址？**
九成是防火墙：首次启动 Windows 弹窗时点了「取消」。到「Windows 安全中心 → 防火墙 → 允许应用通过防火墙」里勾上 Python，或临时关掉防火墙验证。其次确认两台设备在同一个 WiFi / 网段（手机热点也算同一网段）。

**端口 8899 / 8188 被占用**
8899 是网页界面，重复启动只会再开一个浏览器页；8188 是 ComfyUI，若被别的 ComfyUI 占用，请先关掉它。

**支持 Linux / macOS？**
核心逻辑跨平台：`python install.py --skip-deps` + `python download_models.py` + `python MyPixelTool/webui.py`，模型直接放进 `ComfyUI/models/`。`.bat` 仅是 Windows 便利封装。

## 许可证

- 本项目代码：**MIT**（见 `LICENSE`）；第三方组件明细见 `NOTICE`
- `custom_nodes/pixel_memory`：作为 ComfyUI 插件运行，遵循 **GPL-3.0**
- ComfyUI 由安装脚本单独拉取，遵循其自身 **GPL-3.0**，不包含在本仓库中
- 模型权重版权归各自作者所有，本仓库不附带、不再分发；**RMBG 2.0 为非商用许可**，商用请向 BRIA 申请

## 致谢

[ComfyUI](https://github.com/comfyanonymous/ComfyUI) · [Qwen-Image](https://huggingface.co/Comfy-Org/Qwen-Image-2.1) · [FastVideo FastH3](https://huggingface.co/FastVideo/FastVideo-FastH3-Comfy) · [MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3) · [RMBG 2.0](https://github.com/Bria-AI/RMBG-2.0)
