# 模型放这里

本仓库**不包含**模型权重（约 64 GB）。把文件按下面目录放好即可：

```
models/
├── diffusion_models/
│   ├── qwen_image_2.1_turbo_bf16.safetensors                            13.3 GB
│   └── fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors        20.6 GB
├── text_encoders/
│   ├── qwen3vl_8b_fp8_scaled.safetensors                                 9.9 GB
│   └── qwen3vl_32b_minimax_h3_int4_convrot.safetensors                  13.9 GB
├── vae/
│   ├── qwen_image_2.1_vae_bf16.safetensors                               0.6 GB
│   └── minimax_h3_video_vae_fp16.safetensors                             4.9 GB
└── background_removal/
    └── rmbg2.0.safetensors                                               0.8 GB
```

放好之后执行（把上面这些目录「联接」进 ComfyUI，不占双份空间）：

```bash
python MyPixelTool/setup_models.py
```

Windows 也可以双击 `MyPixelTool\初始化模型目录.bat`。

> 也可以不使用 `models/`，直接把文件放进 `ComfyUI/models/<同名目录>/`，效果一样。
>
> 文件名必须保持完全一致，程序按名字加载；少任何一个都会在对应环节报错。
