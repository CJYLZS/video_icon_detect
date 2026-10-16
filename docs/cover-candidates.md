# JEV 候选封面扫描

clip / clip-batch 默认在剪辑、BGM 和战局统计之后执行候选扫描。输入明确为输出目录中的同名主成片（<stem>.mp4），不是原录像或 _bgm 成片。--no-autocut 不关闭候选阶段；--no-cover-candidates 才关闭。

## 配置

- --cover-fps：默认 10，独立于击倒检测 --fps。
- --cover-thresh：默认 0.6；单帧 noul 概率 >= 该值才收集为候选，调高更严、调低更宽。
- --jev-url：默认 http://127.0.0.1:8000/v1/systemone；这是 d1-3B 的专用决策接口，不是 OpenAI Chat Completions。
- --jev-model：默认 jev-d1-3B。
- JEV 服务须由用户预先启动，本工具不启动服务、不下载模型。探测 /v1/models，未启动/不可访问时提示并跳过，不影响剪辑和后续视频。
- 使用仓库 bundled FFmpeg；Python 图像预处理依赖 Pillow，不需要在本仓库安装 VLM 权重或 Transformers。

## 筛选与产物

提示词已硬化：总体风格仍为重火力、强反馈、原生实战感，但要求单帧本身就能看出正在发生的战斗动作，并对爆炸/射击给出直接视觉证据——爆炸需见黄/橙/白高亮火焰或闪光核心，射击需见枪口火焰、弹道或撞击火花；只有黑烟灰烟、远景载具或直升机冒烟、得分与击杀 HUD、字幕提示一律判否。单帧 noul 概率 >= 阈值（默认 0.6，--cover-thresh 可调）即收集，不做排名、去重或数量裁剪。两次 FFmpeg 均用相同 fps 滤镜，第二次在 fps 之后按样本索引导出原分辨率 JPG。时间是采样网格时间，不声称为源帧真实 PTS。

每个视频输出 cover_candidates/，含候选 JPG、scan_summary.json、scan_results.json 和 yes_results.json。全 No 也属于完整扫描。

.cover_candidates_work/ 保存运行锁、配置和逐帧 checkpoint。失败保留断点，不发布半成品；下次重试失败项。源视频/提示词/配置变化或 --force 时重新判定，旧 checkpoint 归档。完整扫描缓存同时校验源文件路径、大小、mtime、模型/参数（含 --cover-fps、--cover-thresh）/提示词及候选文件是否存在。旧候选在新结果完成前保留，替换时归档到工作目录 previous_output_*，不覆盖人工选出的最终封面。

强制终止后若残留 running.lock，先确认进程已结束，再人工移走锁。不要删除仍在使用的锁。

## 手动后续步骤

这里只生成候选素材，不自动选定最终封面，也不在 clip 中触发 ChatGPT 或投稿。人工从 `cover_candidates/` 选一张复制到视频同级目录并命名为 `封面.jpg` 后，可使用仓库内 `scripts/pipeline.mjs` 继续执行「封面美化（可选）→ B 站投稿」。具体参数见 [发布流水线说明](publishing.md)。账号配置、浏览器 profile、运行状态和视频素材不迁移入 Git。

## 实跑：2026.10 的 1018–1024

原始输入位于 F:\video_edit\input_backup\2026，已有输出位于 F:\video_edit\final_backup\2026.10。无需移动或复制视频：逐个 clip 指向原始输入，--output 指向月份根目录，自动命中已有成片、清单、BGM 配方和统计缓存，补跑候选阶段。clip-batch 当前不提供日期范围筛选，避免对整个输入备份目录执行。

在仓库根目录执行以下 PowerShell 命令，可覆盖七个日期；再次执行会复用已完成候选。

```powershell
$env:PYTHONIOENCODING = "utf-8"
foreach ($date in 1018..1024) {
    & .venv\Scripts\python.exe main.py clip `
        --video "F:\video_edit\input_backup\2026\$date.mp4" `
        --output "F:\video_edit\final_backup\2026.10" `
        --fps 10 --missile --no-progress
    if ($LASTEXITCODE -ne 0) { throw "clip failed for $date" }
}
```

本轮续跑实际使用 1018,1019,1020,1021,1023,1024 列表；1022 前一轮已完成。七目录最终核验：

| 日期 | 扫描帧 | 候选 JPG |
|---|---:|---:|
| 1018 | 1360 | 180 |
| 1019 | 1346 | 162 |
| 1020 | 1686 | 203 |
| 1021 | 1826 | 171 |
| 1022 | 1548 | 91 |
| 1023 | 2585 | 255 |
| 1024 | 2022 | 207 |

共 12373 帧、1269 张候选，已逐张验证 JPEG 完整性并核对三个扫描 JSON。后续仍需人工挑选最终封面。

### 大量候选的导出修复

初次实跑暴露 FFmpeg 表达式解析深度限制：连续 eq(n,INDEX) 相加在 150 项的合成复现中失败，并报 Cannot allocate memory。将加法构造成平衡括号树后，1000 项回归通过；不改变样本索引、采样或筛选结果。导出继续使用 fps 后接 select，确保低清判定与原分辨率导出对应同一采样帧。完整单元与 FFmpeg 集成测试共 76 项通过。
