# 模型放这里

本仓库**不包含**模型权重（约 64 GB）。权威清单是 [`manifest.json`](manifest.json)：
每个文件的目标路径、精确字节数、SHA256、官方仓库都在里面。

## 三种放法

**① 自动下载（推荐）**——仓库根目录执行：

```bash
python download_models.py            # 全部必需文件，断点续传
python download_models.py --check    # 只检查缺什么
python download_models.py --only vae # 只下某一部分
```

国内默认走 `hf-mirror.com`，RMBG 2.0 走 ModelScope 直链；失败自动换源。
下完自动校验字节数，加 `--sha` 可做 SHA256 全量校验。

**② 手动下载**——按 manifest 里的 `repo`/`path` 从 HF（或 hf-mirror）下载，
**文件名必须保持与 `target` 完全一致**，放到对应目录：

```
models/
├── diffusion_models/
│   ├── qwen_image_2.1_turbo_bf16.safetensors                     13.3 GB
│   └── fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors 20.6 GB
├── text_encoders/
│   ├── qwen3vl_8b_fp8_scaled.safetensors                          9.9 GB
│   └── qwen3vl_32b_minimax_h3_int4_convrot.safetensors           13.9 GB
├── vae/
│   ├── qwen_image_2.1_vae_bf16.safetensors                        0.6 GB
│   └── minimax_h3_video_vae_fp16.safetensors                      4.9 GB
└── background_removal/
    └── rmbg2.0.safetensors                                        0.8 GB
```

**③ 直接放进 ComfyUI**——把文件放到 `ComfyUI/models/<同名目录>/`，效果一样。

## 放好之后

```bash
python MyPixelTool/setup_models.py      # 把 models/ 联接进 ComfyUI（不复制、不占双份空间）
python download_models.py --check       # 复核：应显示 7 个全部「已就绪」
```

> - 音频 VAE（`minimax_h3_audio_vae_fp32`）本工具用不到，可不下。
> - 大小校验允许 1MB 内的元数据差异；差得多说明文件不对，别硬用。
> - 模型权重版权归各作者；RMBG 2.0 为非商用许可，商用需向 BRIA 申请。
