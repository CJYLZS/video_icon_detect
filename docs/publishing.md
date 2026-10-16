# 封面生成与 B 站投稿流水线

本仓库已迁移旧仓库的三个手动发布工具：

- `scripts/chatgpt_cover.mjs`：使用 ChatGPT 网页版生成美化封面，支持登录态复用、多账号额度轮换、断点续跑。
- `scripts/bili_upload.mjs`：使用独立 Edge profile 打开 B 站投稿页，填写标题、封面、分区、声明和定时发布时间。
- `scripts/pipeline.mjs`：组合「补封面 → 投稿」两个阶段；默认只校验素材，不会真正投稿。

## 依赖

先安装 Node 依赖：

```powershell
npm install
```

首次使用前复制配置模板并填写本机 ChatGPT profile：

```powershell
Copy-Item config\cg_profiles.example.json cg_profiles.json
```

真实的 `cg_profiles.json`、浏览器 profile、登录态和运行状态不会加入 Git。

## 从候选封面继续

`clip` 完成后，候选图位于：

`<月份目录>\<日期>\cover_candidates\`

人工挑选一张复制到视频同级目录，并固定命名为：

`封面.jpg`

发布工具读取优先级为：

1. ChatGPT 生成的 `封面_chatgpt.png` 或 UUID PNG（同档多张取 mtime 最新的一张）；
2. 同级目录的人工定稿 `封面.jpg`；
3. 原始 `封面.png`（仅使用 `--allow-raw-cover` 时允许作为投稿兜底）。

视频使用同级的 `<目录名>_bgm.mp4`，标题使用同级的 `标题.txt`。

## 使用流水线

先只看计划，不启动实际投稿：

```powershell
node scripts/pipeline.mjs --root "F:\video_edit\final_backup\2026.10" --dirs 1018-1024 --plan
```

只生成 ChatGPT 美化封面：

```powershell
node scripts/pipeline.mjs --root "F:\video_edit\final_backup\2026.10" --dirs 1018-1024 --cover-only
```

只校验投稿素材：

```powershell
node scripts/pipeline.mjs --root "F:\video_edit\final_backup\2026.10" --dirs 1018-1024 --upload-only
```

确认标题、封面、视频和发布时间无误后，明确添加 `--submit` 才会点击 B 站的投稿按钮：

```powershell
node scripts/pipeline.mjs --root "F:\video_edit\final_backup\2026.10" --dirs 1018-1024 --submit --topic "和平精英激战金秋"
```

常用选项：

- `--force`：忽略已投稿日期，允许重投。
- `--allow-raw-cover`：封面美化失败时允许使用原始封面；默认不允许。
- `--profile acct1`：指定 ChatGPT 账号。
- `--bili-port 9235`：指定 B 站自动化 Edge 的 CDP 端口。
- `--time 19:00`：设置定时发布时间。

流水线会查询 B 站已有稿件，默认跳过已占用的发布日期；查询失败时会停止投稿阶段，避免重复投稿。
