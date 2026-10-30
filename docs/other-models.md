# 其他底模接入说明（moss 等非 Anima 系模型）

适用版本：画舫 v0.1.1 及以后。

画舫内置的工作流是 Anima 架构。想用其他底模（例如 moss，或其它架构的模型），需要自备一条**用该底模能正常出图的 ComfyUI 工作流**，导入画舫后使用。本文说明完整流程与已知限制。

## 一、先放模型

| 模型文件 | 放到 ComfyUI 的 |
| --- | --- |
| unet 分离格式底模（给 UNETLoader 用的 `.safetensors`） | `models/diffusion_models/` |
| 整包 checkpoint | `models/checkpoints/`（注意：见「五、已知限制」第 1 条） |
| 文本编码器（TE） | `models/text_encoders/` |
| VAE | `models/vae/` |

模型、权重、词表都需要自备，仓库不提供下载。

## 二、在 ComfyUI 里准备一条可用工作流

1. 用目标底模搭一条完整出图流（加载底模、正负词、KSampler、SaveImage），先确认它在本机 ComfyUI 里能出图。
2. 打开 ComfyUI 设置，启用 **Dev mode Options**。
3. 用 **「Save (API Format)」** 导出 JSON。

> 注意：普通的「Save」导出的是界面格式，画舫不认；导入时会提示「这是 ComfyUI 界面导出的（Save）」。

## 三、导入画舫

在画舫项目根目录运行：

```powershell
.\.venv\Scripts\python.exe tools\import_workflow.py 你的工作流.json --id moss_t2i --label "Moss 直出" --family moss
```

- `--id`：工作流标识（字母数字下划线）；不填则用文件名。
- `--label`：界面下拉里显示的名字。
- `--family`：模型家族，推断不出来，建议手动给。

产物与报告：

- `workflows/_staging/<id>.json`：导入结果（半成品，不会直接被运行中的服务加载）。
- `acceptance/B-map-<id>.md`：映射报告，列出**自动识别项**与**需人工修项**。

> 报告里「需人工修」超过 2 处时，不建议直接使用——说明这条工作流的结构与画舫的契约对得不够齐。

## 四、按报告修好并启用

1. 逐条处理报告里「需人工修」的项（底模／CLIP／VAE 槽、正负词连线、尺寸来源、存图口等）。
2. 把改好的 JSON 从 `workflows/_staging/` 移动到 `workflows/`。
3. 重启画舫。工作流下拉会出现新条目，选中它。
4. 在「底模」下拉选择目标模型（只列出 `diffusion_models` 里的文件），填写提示词与参数，出图。
5. 满意后用「整套载入」保存为预设，之后一键复用。

## 五、已知限制

1. **checkpoint 目录错配**：画舫的「底模」下拉目前只扫描 `models/diffusion_models`。整包 checkpoint（放在 `models/checkpoints`）不会出现在下拉里。请优先使用 unet 分离格式的底模；checkpoint 支持尚未纳入。
2. **架构必须匹配**：内置的 Anima 工作流不能直接换其他架构的底模；换底模请使用对应工作流，否则契约检查或出图环节会拦截。
3. **只接管标准节点**：画舫只接管工作流里的采样器参数、尺寸、正负词、存图等标准节点；认不准的连线会在报告里留空，需要人工补。
4. 本说明不改变「模型自备、仓库不提供模型与权重」的基本约定。

## 六、常见问题

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 「底模」下拉里没有 moss | 文件不在 `diffusion_models`，或格式是 checkpoint | 换 unet 分离版放进 `diffusion_models`；checkpoint 见限制 1 |
| 导入时报「这是 ComfyUI 界面导出的（Save）」 | 导出格式不对 | 开启 Dev mode，用 Save (API Format) 重新导出 |
| 导入成功但界面提示缺模型 | 工作流里的文件名与本机实际文件名不一致 | 按报告人工修，或把工作流里的文件名改成实际文件名 |
| 切换工作流后仍提示缺模型 | 空模型演示，或模型没有放对目录 | 确认模型已就位并重启画舫 |
| 「需人工修」超过 2 处 | 工作流结构画舫认不准 | 简化工作流，或按报告逐条补连线与槽位 |

遇到本文没覆盖的问题，可以在仓库提 Issue，附上映射报告里对应的段落。