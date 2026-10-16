#!/usr/bin/env node
/**
 * bili_upload.mjs —— B 站视频投稿全自动化（Playwright + 复用登录态）
 *
 * 流程（对应目录 <MMDD>，如 0918 表示 9 月 18 日发布）：
 *   1. 连自动化 Edge（CDP :9235，--port 可改）：浏览器已在就复用它的登录态和标签页，
 *      没在就自己起一个（复用逻辑同 chatgpt_cover.mjs，不必先跑 start-bili-edge.cmd）。
 *      只认「自己的」浏览器：端口上必须是 msedge + 本工具的 --user-data-dir，
 *      被别的程序（chatgpt 的浏览器、系统里的 Chrome）占着就直接报错，绝不串 session。
 *   2. 打开创作中心投稿页，注入 <目录名>_bgm.mp4
 *   3. 填标题（标题.txt）、加封面（4:3 比例图上，先勾「双比例同步改动」）、
 *      选创作声明（内容为自制：未经作者允许，禁止转载）、选分区（游戏）
 *   4. 开启定时发布，把日期设为目录名对应日期，时间设为 19:00
 *   5. 可选：加参与话题（--topic 精确话题名）
 *   6. 等视频上传完成，再停在「立即投稿」前 —— 默认不提交，加 --submit 才点
 *
 * 封面优先用 chatgpt_cover.mjs 的美化产物（<uuid>.png / 封面_chatgpt.png，取最新的一张），
 * 都没有才回退原始截图 封面.png。
 *
 * 素材目录既可写 4 位日期目录名（相对工作区根目录），也可写绝对/相对路径，
 * 素材留在剪辑盘上时直接指过去即可，不必往工作目录拷一份或建链接。
 *
 * 用法：
 *   —— 单个目录 ——
 *   node bili_upload.mjs 0918              # 演练到填完所有字段（不提交）
 *   node bili_upload.mjs 0918 --submit     # 填完并提交
 *   node bili_upload.mjs "F:/video_edit/final_backup/0920"                      # 用绝对路径读素材
 *   node bili_upload.mjs "F:/video_edit/final_backup/0920" --date 2026-09-20    # 目录名非 MMDD 时必须给 --date
 *   —— 批量（根目录 + 一级目录，逻辑同 chatgpt_cover.mjs）——
 *   node bili_upload.mjs --root "F:/video_edit/final_backup/2026.10" --dirs 1001-1010 --plan
 *   node bili_upload.mjs --root "F:/video_edit/final_backup/2026.10" --dirs 1004,1005 --submit
 *   node bili_upload.mjs "F:/video_edit/final_backup/2026.10" --dirs 1001-1010 --submit   # 根目录也可当位置参数
 *   node bili_upload.mjs --root "F:/..." --all --plan                       # 根目录下所有一级目录
 *   —— 其他 ——
 *   node bili_upload.mjs 0918 --topic 和平精英激战金秋        # 参与话题
 *   node bili_upload.mjs 0918 --time 19:00                  # 覆盖发布时间（默认 19:00）
 *   node bili_upload.mjs 0918 --upload-wait 0               # 提交前不等视频上传完成（默认等 30 分钟）
 *   node bili_upload.mjs --attach-only                      # 只起/连浏览器（首次登录用它），不投稿
 *   node bili_upload.mjs 0918 --port 9231                   # 换 CDP 端口（默认 9235）
 *   node bili_upload.mjs 0918 --login-wait 0                # 未登录时不等待（默认等 5 分钟）
 *   node bili_upload.mjs --root "F:/..." --dirs 1001-1010 --plan --json --with-scheduled  # 给流水线读的计划
 *
 * 整套流程（缺封面就先生成封面，再投稿）用 pipeline.mjs，见该脚本头部说明。
 *
 * 批量说明：
 *   · --dirs 支持 1001-1010 这种连续简写；--all 取根目录下全部一级目录
 *   · 发布年份优先取根目录名里的年份（根目录叫 2026.10 → 子目录 1004 就是 2026-10-04），
 *     根目录名没有年份时才按「MMDD + 当前年（过去超半年则顺延一年）」推
 *   · 逐条串行：等上一个视频上传完才动下一个，总时长约等于各视频上传时间之和
 *   · 加 --submit 才真投稿；不加只校验素材 + 列计划（批量演练不会逐个白传视频、留一堆草稿）
 *   · 某个目录失败不打断整批，最后给汇总；退出码 0 = 全成功，2 = 有失败或结果未确认
 *   · 复投判定看 B 站稿件列表：该日期已经有稿件（定时发布中或已发布）就跳过，日志写明撞的是哪条；
 *     另外提交成功会在素材目录写 .bili_uploaded.json 作为本地记录，没有在线检查时兜底；
 *     两者都可以用 --force 无视（重投）
 *
 * 给 pipeline.mjs 用的接口（也方便自己排查）：
 *   --plan --json            把计划以 JSON 打到 stdout（人看的日志走 stderr），含每个目录的
 *                            日期/标题/封面路径/coverKind(polished|raw|null)
 *   --plan --with-scheduled  顺带连浏览器查 B 站稿件列表，JSON 里多一个 scheduled 字段
 *                            （{ok:true,days:{日期:稿件}} 或 {ok:false,error}；没查是 null）
 *   --result-json <路径>     跑完把每条结果写成 JSON 文件（批次/单目录、成功/失败/未确认/跳过）
 *   --port <n>               首选 CDP 端口（默认 9235）；本工具的浏览器已经在别的端口上跑着时
 *                            会自动复用它（按 --user-data-dir 认，不按端口号猜）；
 *                            端口被别的浏览器占用或被 Windows 保留时直接报错并给出可用端口
 *   --plan --json 里还会带 browserPort：浏览器最终落在哪个端口（调用方防串端口用）
 *
 * 浏览器：自动化专用 profile（%LOCALAPPDATA%\bili-automation-profile，不碰日常 Edge），
 * 首次跑会弹窗，登录一次 B 站即可，之后登录态长期有效。
 */
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_DIR = path.resolve(__dirname, '..');

// ───────────────────────── 配置 ─────────────────────────
const UPLOAD_PAGE = 'https://member.bilibili.com/platform/upload/video/frame';
const LOGIN_PAGE = 'https://passport.bilibili.com/login';
const VIDEO_EXT = '.mp4';

// 默认端口 9235：避开 chatgpt 侧的 9222-9234，也在 Windows 保留段（9238-9337 等）之外。
// 真正认命中的是 --user-data-dir（见 pickPort）：浏览器在别的端口上跑着就复用它。
const DEFAULT_PORT = 9235;
// 自动化专用 profile（与 start-bili-edge.cmd 同一个）：不碰日常 Edge 的配置和登录态
const PROFILE_DIR = process.env.LOCALAPPDATA
  ? path.join(process.env.LOCALAPPDATA, 'bili-automation-profile')
  : path.join(WORKSPACE_DIR, '.bili-automation-profile');

const STATEMENT_TEXT = '内容为自制：未经作者允许，禁止转载';
const PARTITION = '游戏';

const args = process.argv.slice(2);
const SUBMIT = args.includes('--submit');
const ATTACH_ONLY = args.includes('--attach-only'); // 只起/连浏览器，不读素材也不投稿
const PLAN = args.includes('--plan');               // 只校验素材并列计划，不碰浏览器
const FORCE = args.includes('--force');             // 批量时重投已有投稿记录的目录
const ALL = args.includes('--all');                 // 批量：根目录下所有一级目录
const JSON_OUT = args.includes('--json');           // 配合 --plan：只输出 JSON（给 pipeline.mjs 用）
const WITH_SCHEDULED = args.includes('--with-scheduled'); // 配合 --plan：连 B 站查哪些日期已排稿件
const argVal = (flag, dflt) => {
  const i = args.indexOf(flag);
  return i >= 0 && args[i + 1] && !args[i + 1].startsWith('--') ? args[i + 1] : dflt;
};
const TIME = argVal('--time', '19:00');
const DATE_OVERRIDE = argVal('--date', null);
const TOPIC = argVal('--topic', null); // 可选：参与话题的精确名称
const UPLOAD_WAIT_MIN = Number(argVal('--upload-wait', '30')); // 提交前等视频上传完成的上限（分钟），0 = 不等
const CDP_PORT = Number(argVal('--port', String(DEFAULT_PORT)));
const LOGIN_WAIT_MIN = Number(argVal('--login-wait', '5')); // 未登录时在窗口里等登录的分钟数，0 = 直接退出
const ROOT_ARG = argVal('--root', null);                    // 批量：根目录
const DIRS_SPEC = argVal('--dirs', null)?.split(',').map((s) => s.trim()).filter(Boolean) ?? [];
const RESULT_JSON = argVal('--result-json', null);          // 跑完把结果写成 JSON（给 pipeline.mjs 用）
const BATCH_SPEC = !!ROOT_ARG || DIRS_SPEC.length > 0 || ALL;

// 目标目录 = 位置参数，两种写法都行：
//   ① 4 位日期目录名 —— 相对脚本目录，如 0920
//   ② 绝对/相对路径 —— 素材留在剪辑盘上时直接指过去，不必往工作目录拷或建链接
// 批量模式下位置参数也可以当根目录用（--dirs/--all 在场时）
const VALUE_OPTS = ['--time', '--date', '--topic', '--upload-wait', '--port', '--login-wait', '--root', '--dirs', '--result-json'];
const optValueIdx = new Set(VALUE_OPTS.map((f) => args.indexOf(f) + 1).filter((i) => i > 0));
const TARGET_ARG = args.find((a, i) => !a.startsWith('--') && !optValueIdx.has(i));
if (!TARGET_ARG && !ATTACH_ONLY && !BATCH_SPEC) {
  console.error('用法: node bili_upload.mjs <MMDD | 目录路径> [--submit] [--date YYYY-MM-DD] [--time HH:MM] [--topic 话题名]');
  console.error('      node bili_upload.mjs --root <根目录> --dirs 1001-1010 [--all] [--submit] [--plan]');
  console.error('  例: node bili_upload.mjs 0920');
  console.error('  例: node bili_upload.mjs "F:/video_edit/final_backup/0920"');
  console.error('  例: node bili_upload.mjs --root "F:/video_edit/final_backup/2026.10" --dirs 1001-1010 --plan');
  console.error('  例: node bili_upload.mjs --attach-only   # 只起浏览器并登录，不投稿');
  process.exit(1);
}

let INDENT = ''; // 批量模式给每条流程的日志加缩进，方便翻看
// --json（配合 --plan）时 stdout 只留 JSON 给调用方解析，人看的日志全走 stderr
const log = (...a) => (JSON_OUT ? console.error : console.log)(INDENT + a.join(' '));
const die = (m) => { console.error('✗ ' + m); process.exit(1); };
const fail = (m) => { throw new Error(m); }; // 单条流程内的失败：批量时只中断这一条，不杀整批
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ───────────────────────── 素材解析 ─────────────────────────
const YEAR_MONTH = /^(\d{4})[.\-_](\d{1,2})$/; // 根目录名形如 2026.10 / 2026-10

// 发布日期：目录名是 MMDD，年份优先取根目录名里的年份（2026.10 → 2026），
// 拿不到才按「当前年，过去超半年则顺延一年」猜（原来是这么干的，单独跑一个目录时用得上）
function inferPublishDate(dirName, rootName) {
  if (DATE_OVERRIDE) return { date: DATE_OVERRIDE, warnings: [] };
  const m = /^(\d{2})(\d{2})$/.exec(dirName);
  if (!m) {
    fail(`目录名 "${dirName}" 不是 MMDD 格式，无法推断发布日期${BATCH_SPEC ? '' : '，请加 --date YYYY-MM-DD'}`);
  }
  const mm = Number(m[1]);
  const dd = Number(m[2]);
  const warnings = [];

  const rm = rootName ? YEAR_MONTH.exec(rootName) : null;
  let year;
  if (rm) {
    year = Number(rm[1]);
    if (Number(rm[2]) !== mm) warnings.push(`根目录名 "${rootName}" 的月份与目录名 ${dirName} 不一致，按目录名的 ${mm} 月算`);
  } else {
    const now = new Date();
    year = now.getFullYear();
    if (now.getTime() - new Date(year, mm - 1, dd).getTime() > 180 * 86400 * 1000) year += 1;
  }

  const date = `${year}-${String(mm).padStart(2, '0')}-${String(dd).padStart(2, '0')}`;
  if (new Date(year, mm - 1, dd, 23, 59, 59).getTime() < Date.now()) {
    warnings.push(`发布时间 ${date} ${TIME} 已经过去，B 站的定时发布大概率不接受`);
  }
  return { date, warnings };
}

// 解析一个目标目录的素材。失败就 fail（批量时由调用方记成「跳过」，不打断整批）
function loadTarget(dir, rootName) {
  if (!existsSync(dir)) fail(`目录不存在: ${dir}`);
  if (!statSync(dir).isDirectory()) fail(`不是目录: ${dir}`);
  const dirName = path.basename(dir);

  // 视频固定取 <目录名>_bgm.mp4；目录名与文件前缀不一致时，若目录里只有一个 *_bgm.mp4 就用它
  let videoPath = path.join(dir, `${dirName}_bgm${VIDEO_EXT}`);
  if (!existsSync(videoPath)) {
    const bgm = readdirSync(dir).filter((f) => f.endsWith(`_bgm${VIDEO_EXT}`));
    if (bgm.length !== 1) {
      fail(`未找到带 BGM 的视频: ${videoPath}${bgm.length ? `（目录里有 ${bgm.length} 个 *_bgm.mp4，无法确定用哪个）` : ''}`);
    }
    videoPath = path.join(dir, bgm[0]);
  }

  const titleFile = path.join(dir, '标题.txt');
  if (!existsSync(titleFile)) fail(`未找到标题文件: ${titleFile}`);
  const title = readFileSync(titleFile, 'utf8').trim();
  if (!title) fail('标题.txt 为空');

  // 封面：ChatGPT 美化产物优先；没有美化产物时才用人工挑选的 封面.jpg，
  // 最后回退原始截图 封面.png。
  // 同一档多张时取 mtime 最新的一张。
  let coverPath = null;
  let coverKind = null; // polished = 已美化（chatgpt 产物）；raw = 只有原始截图
  {
    const files = readdirSync(dir);
    const UUID_PNG = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.png$/i;
    const manual = files.filter((f) => f === '封面.jpg');
    const polished = files.filter((f) => UUID_PNG.test(f) || f === '封面_chatgpt.png');
    const pool = polished.length ? polished : (manual.length ? manual : files.filter((f) => f === '封面.png'));
    if (pool.length) {
      const newest = pool
        .map((f) => ({ f, t: statSync(path.join(dir, f)).mtimeMs }))
        .sort((a, b) => b.t - a.t)[0];
      coverPath = path.join(dir, newest.f);
      coverKind = polished.length ? 'polished' : 'raw';
    }
  }

  const { date: publishDate, warnings } = inferPublishDate(dirName, rootName);
  const videoMB = Number((statSync(videoPath).size / 1048576).toFixed(0));
  return { dir, dirName, videoPath, videoMB, title, coverPath, coverKind, publishDate, warnings };
}

// ───────────────────────── 批量目标解析 ─────────────────────────
// 投稿记录：提交成功后落在素材目录里，批量模式靠它跳过已投过的目录，中断后能直接重跑续跑
const MARKER = '.bili_uploaded.json';
const markerPath = (dir) => path.join(dir, MARKER);
function readMarker(dir) {
  try { return JSON.parse(readFileSync(markerPath(dir), 'utf8')); } catch { return null; }
}
function writeMarker(a, outcome) {
  const rec = {
    submittedAt: new Date().toISOString(),
    title: a.title, publishDate: a.publishDate, publishTime: TIME,
    video: path.basename(a.videoPath), outcome,
  };
  try {
    writeFileSync(markerPath(a.dir), JSON.stringify(rec, null, 2) + '\n', 'utf8');
    log(`      已记录投稿（${MARKER}）`);
  } catch (e) { log(`      (!) 写 ${MARKER} 失败: ${e.message}`); }
}

// 支持形如 1001-1010 的连续编号简写（保留前导零），同 chatgpt_cover.mjs
function expandDirSpec(spec) {
  const m = String(spec).match(/^(\d+)-(\d+)$/);
  if (!m) return [spec];
  const [, a, b] = m;
  const from = Number(a), to = Number(b);
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return [spec];
  if (to - from > 200) fail(`目录范围过大：${spec}`);
  const out = [];
  for (let i = from; i <= to; i++) out.push(String(i).padStart(a.length, '0'));
  return out;
}

function resolveTargets({ allowEmpty = false } = {}) {
  // 单目录：位置参数。父目录名能提供年份就提供（…/2026.10/1004 直接指过来时年份对得上）
  if (!BATCH_SPEC) {
    if (!TARGET_ARG) fail('请给出日期目录/路径，或用 --root + --dirs 指定批量目标');
    const dir = /^\d{4}$/.test(TARGET_ARG) ? path.join(WORKSPACE_DIR, TARGET_ARG) : path.resolve(TARGET_ARG);
    return { batch: false, root: path.dirname(dir), targets: [loadTarget(dir, path.basename(path.dirname(dir)))], skipped: [] };
  }

  if (DATE_OVERRIDE) fail('批量模式不能用 --date（每条日期由目录名推，年份取根目录名），要指定日期请单目录跑');
  let root = ROOT_ARG ? path.resolve(ROOT_ARG) : null;
  if (!root && TARGET_ARG) root = path.resolve(TARGET_ARG); // 根目录也可以直接当位置参数
  if (!root) root = WORKSPACE_DIR;
  if (!existsSync(root) || !statSync(root).isDirectory()) fail(`根目录不存在：${root}`);

  let names;
  if (DIRS_SPEC.length) names = DIRS_SPEC.flatMap(expandDirSpec);
  else if (ALL) {
    names = readdirSync(root).filter((d) => {
      try { return statSync(path.join(root, d)).isDirectory(); } catch { return false; }
    }).sort();
  } else fail('批量模式需要 --dirs <目录名列表> 或 --all');

  const seen = new Set();
  const targets = [];
  const skipped = [];
  for (const n of names) {
    const dir = path.isAbsolute(n) ? n : path.join(root, n);
    const name = path.basename(dir);
    if (seen.has(name)) { skipped.push({ name, why: '重复指定' }); continue; }
    seen.add(name);

    if (!FORCE && existsSync(markerPath(dir))) {
      const m = readMarker(dir);
      const when = m?.submittedAt ? `，${String(m.submittedAt).slice(0, 10)} 投的` : '';
      skipped.push({ name, why: `已投稿过${when}（要重投加 --force）` });
      continue;
    }
    try { targets.push(loadTarget(dir, path.basename(root))); }
    catch (e) { skipped.push({ name, why: e.message }); }
  }
  if (!targets.length && !allowEmpty) {
    fail([`没有可处理的目录（跳过 ${skipped.length} 个）：`, ...skipped.map((s) => `  · ${s.name}：${s.why}`)].join('\n'));
  }
  return { batch: true, root, targets, skipped };
}

// 把当前批量参数拼回一条命令，用于提示「怎么真投稿」
function batchSpecArg() {
  const parts = [];
  if (ROOT_ARG) parts.push(`--root "${ROOT_ARG}"`);
  else if (TARGET_ARG) parts.push(`"${TARGET_ARG}"`);
  if (DIRS_SPEC.length) parts.push(`--dirs ${DIRS_SPEC.join(',')}`);
  else if (ALL) parts.push('--all');
  return parts.join(' ');
}

function printTarget(t, i, total) {
  if (BATCH_SPEC) log(`──── [${i + 1}/${total}] ${t.dirName}  ${t.publishDate} ${TIME}`);
  else log(`素材检查: ${t.dirName}`);
  log(`  目录  : ${t.dir}`);
  log(`  视频  : ${t.videoPath} (${t.videoMB} MB)`);
  log(`  标题  : ${t.title}`);
  log(`  封面  : ${t.coverPath ?? '(无)'}`);
  log(`  发布  : ${t.publishDate} ${TIME}`);
  for (const w of t.warnings) log(`  (!) ${w}`);
}

// ───────────────────────── 浏览器会话 ─────────────────────────
// 复用逻辑同 chatgpt_cover.mjs：先探测 CDP 端口，没人听就用自动化专用 profile
// 起一个 Edge（detached，脚本退出后继续活着），有人听就直接复用它的登录态和标签页。
//
// 端口归属靠进程命令行认，不靠端口号猜：本工具的 profile 可能已经在别的端口上跑着
// （上一轮用过 --port），而 :9222 也常被 chatgpt_cover.mjs 的 acct1 占着 ——
// 认 profile 就能两边都不撞车，两个阶段各用各的 session。
const cdpUrl = (port) => `http://127.0.0.1:${port}`;
const normPath = (p) => path.resolve(p).replace(/[\\/]+$/, '').toLowerCase();
// 手工用 start-bili-edge.cmd 起浏览器时，命令行里是字面量的 %LOCALAPPDATA%，比路径前先展开
const expandWin = (p) => p.replace(/%([^%]+)%/g, (m, k) => process.env[k] ?? m);
const grabArg = (cmdline, re) => { const m = cmdline.match(re); return m ? (m[1] ?? m[2]) : null; };

function findEdge() {
  const cands = [
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  ].filter(Boolean);
  for (const c of cands) if (existsSync(c)) return c;
  die('找不到 msedge.exe');
}

async function cdpAlive(port) {
  try {
    const r = await fetch(`${cdpUrl(port)}/json/version`, { signal: AbortSignal.timeout(1500) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

// 列出正在运行的 Edge 主进程（排除渲染/工具/崩溃处理子进程，它们也带 --user-data-dir）。
// 查不到（非 Windows、没权限、PowerShell 被挡）返回 null —— 调用方按「不知道」处理。
async function listEdgeProcesses() {
  if (process.platform !== 'win32') return null;
  const { execFile } = await import('node:child_process');
  const { promisify } = await import('node:util');
  const ps = `Get-CimInstance Win32_Process -Filter "Name='msedge.exe'"`
    + ` | Where-Object { $_.CommandLine -notmatch '--type=' }`
    + ` | Select-Object -ExpandProperty CommandLine`;
  let out = '';
  try {
    const { stdout } = await promisify(execFile)('powershell', ['-NoProfile', '-Command', ps], { timeout: 20000 });
    out = stdout || '';
  } catch { return null; }
  return out.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const m = l.match(/--user-data-dir=(?:"([^"]+)"|([^\s"]+))/);
    return {
      cmdline: l,
      port: Number((l.match(/--remote-debugging-port=(\d+)/) || [])[1]) || null,
      dir: m ? expandWin(m[1] ?? m[2]) : null,
    };
  });
}

// :port 上是谁在听？必须是本工具的 Edge 主进程才算自己的 ——
// 别的 Edge/Chrome（比如 chatgpt 的浏览器、系统里跑的无头 Chrome）占着就不认。
async function portListener(port) {
  if (process.platform !== 'win32') return { state: 'unknown' };
  const { execFile } = await import('node:child_process');
  const { promisify } = await import('node:util');
  const ps = `$op=(Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess);`
    + ` if(-not $op){ 'NONE'; exit };`
    + ` $p=Get-CimInstance Win32_Process -Filter "ProcessId=$op" -ErrorAction SilentlyContinue;`
    + ` if(-not $p){ 'UNKNOWN'; exit };`
    + ` $p.Name; $p.CommandLine`;
  let out = '';
  try {
    const { stdout } = await promisify(execFile)('powershell', ['-NoProfile', '-Command', ps], { timeout: 20000 });
    out = stdout || '';
  } catch { return { state: 'unknown' }; }
  const lines = out.split('\n').map((s) => s.trim()).filter(Boolean);
  if (!lines.length || lines[0] === 'UNKNOWN') return { state: 'unknown' };
  if (lines[0] === 'NONE') return { state: 'none' };
  const [name, cmdline = ''] = lines;
  const raw = grabArg(cmdline, /--user-data-dir=(?:"([^"]+)"|([^\s"]+))/);
  const who = `${name}${raw ? `（--user-data-dir=${expandWin(raw)}）` : ''}`;
  const mine = /^msedge\.exe$/i.test(name) && !!raw && normPath(expandWin(raw)) === normPath(PROFILE_DIR);
  return { state: mine ? 'mine' : 'foreign', who };
}

/** 端口能不能绑？null = 能用，否则返回原因（被占 / 被 Windows 保留）。 */
async function portBindError(port) {
  const net = await import('node:net');
  return await new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', (e) => resolve(
      e.code === 'EACCES'
        ? '被 Windows 保留（Hyper-V/动态端口排除占的，netsh int ipv4 show excludedportrange 可查）'
        : `被占用（${e.code}）`));
    srv.once('listening', () => srv.close(() => resolve(null)));
    srv.listen(port, '127.0.0.1');
  });
}

/** 换端口时给个能用的建议值（扫得远一点：Windows 保留段是成片的） */
async function suggestPort(from) {
  for (let p = from + 1; p <= from + 50; p++) if (!(await portBindError(p))) return p;
  return DEFAULT_PORT;
}

/** 端口没响应 → 独立起一个 Edge（detached，脚本退出后它继续活着） */
async function startBrowser() {
  const exe = findEdge();
  const fresh = !existsSync(path.join(PROFILE_DIR, 'Default')); // 全新 profile 会带一堆 Edge 引导页
  log(`      端口 :${CDP_PORT} 没响应 → 启动自动化 Edge`);
  log(`      ${exe} --user-data-dir="${PROFILE_DIR}"`);
  const child = spawn(exe, [
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${PROFILE_DIR}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
    UPLOAD_PAGE,
  ], { detached: true, stdio: 'ignore', windowsHide: false });
  child.unref();

  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const v = await cdpAlive(CDP_PORT);
    if (v) {
      // 端口活了不代表就是刚启动的进程：被别的浏览器抢先占住时，新进程绑不上端口。
      // 认一下占用者，别把投稿页开进 chatgpt 之类的 session 里
      const own = await portListener(CDP_PORT);
      if (own.state === 'foreign') {
        die([
          `:${CDP_PORT} 上的 CDP 不是刚启动的本工具 Edge（${own.who}）——端口被抢占了。`,
          `  换个端口再跑：--port ${await suggestPort(CDP_PORT)}`,
        ].join('\n'));
      }
      log(`      CDP 就绪 ✓（${v.Browser}）`); return { fresh };
    }
    await sleep(1000);
  }
  die([
    `Edge 起来了但 CDP 端口 :${CDP_PORT} 连不上。常见原因：`,
    `  · "${PROFILE_DIR}" 已被别的 Edge 实例占用 —— 同 profile 的窗口还开着时，新进程只把参数转过去，端口不会打开`,
    `    （手动开过这个 profile 的 Edge？把它完全关掉再跑）`,
    `  · 启动时弹了对话框挡着，去桌面看一眼`,
  ].join('\n'));
}

/**
 * 决定这一轮用哪个 CDP 端口：
 *   ① 本 profile 的浏览器已经在跑（任意端口）→ 复用那个端口，不再新开
 *   ② 没在跑，而 --port 指定的端口被别的浏览器（如 chatgpt 的）占着 → 报错，让换端口
 *   ③ 都没占 → 在 --port 上起一个自己的
 */
async function pickPort() {
  const procs = await listEdgeProcesses();
  const mine = procs?.find((p) => p.dir && normPath(p.dir) === normPath(PROFILE_DIR));
  if (mine) {
    if (mine.port && await cdpAlive(mine.port)) {
      if (mine.port !== CDP_PORT) log(`      本工具的浏览器在 :${mine.port}（不是 --port 的 :${CDP_PORT}），复用它`);
      else log(`      复用已在运行的浏览器 :${mine.port}`);
      return { port: mine.port, started: null };
    }
    if (!mine.port) {
      die([
        `本工具的 profile 已被一个没开调试端口的 Edge 占着：`,
        `  "${PROFILE_DIR}"`,
        `  把它完全关掉再跑（同 profile 的新进程只会把参数转给它，CDP 端口起不来）。`,
      ].join('\n'));
    }
  }
  const alive = await cdpAlive(CDP_PORT);
  if (alive) {
    const own = await portListener(CDP_PORT);
    die([
      `端口 :${CDP_PORT} 上不是本工具的 Edge${own.state === 'foreign' ? `：${own.who}` : ''}`,
      `  它被别的用途占着（chatgpt_cover 的浏览器、系统里的 Chrome 等），换个端口：`,
      `    node bili_upload.mjs ${TARGET_ARG ?? '<目录>'} --port ${await suggestPort(CDP_PORT)}`,
      `  或者先把它关掉再跑。`,
    ].join('\n'));
  }
  // 端口虽然没人听，但可能被 Windows 保留（bind 会 EACCES），先探一下别干等 60 秒超时
  const bindErr = await portBindError(CDP_PORT);
  if (bindErr) {
    die([
      `端口 :${CDP_PORT} ${bindErr}，起不了浏览器。`,
      `  换个端口再跑：--port ${await suggestPort(CDP_PORT)}`,
    ].join('\n'));
  }
  return { port: CDP_PORT, started: await startBrowser() };
}

async function sessData(context) {
  const cookies = await context.cookies('https://member.bilibili.com');
  return cookies.find((c) => c.name === 'SESSDATA') ?? null;
}

/** 没登录态就在窗口里等手动登录，SESSDATA 一出现就继续 */
async function ensureLogin(context) {
  const sess = await sessData(context);
  if (sess) { log(`      登录态有效 ✓（SESSDATA httpOnly=${sess.httpOnly}）`); return; }
  if (LOGIN_WAIT_MIN <= 0) die('未登录：请在自动化 Edge 里登录 B 站后重试（--login-wait 0 = 不等待）');

  const page = context.pages()[0] ?? await context.newPage();
  await page.goto(LOGIN_PAGE, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  await page.bringToFront().catch(() => {});
  console.error(`
┌───────────────────────────────────────────────────────────────┐
│  请在自动化 Edge 窗口里登录 B 站（首次跑会有这一步）          │
│  登录成功后脚本自动继续，最长等 ${String(LOGIN_WAIT_MIN).padEnd(2)} 分钟                       │
│  登录态长期保存在该 profile 里，以后不用再登。                │
└───────────────────────────────────────────────────────────────┘`);

  const deadline = Date.now() + LOGIN_WAIT_MIN * 60_000;
  let lastTick = 0;
  while (Date.now() < deadline) {
    if (await sessData(context)) { log('      检测到登录成功 ✓'); await sleep(2000); return; }
    if (Date.now() - lastTick > 15000) {
      lastTick = Date.now();
      log(`      等待登录中… 剩余 ${Math.ceil((deadline - Date.now()) / 60000)} 分钟`);
    }
    await sleep(2000);
  }
  die(`等待登录超时（${LOGIN_WAIT_MIN} 分钟）。登录后重跑本脚本即可，浏览器和登录态都留着`);
}

async function attach() {
  log(`[1/11] 自动化 Edge: ${cdpUrl(CDP_PORT)}`);
  const { port, started } = await pickPort();

  const browser = await chromium.connectOverCDP(cdpUrl(port), { timeout: 30000 })
    .catch((e) => die(`连 CDP 失败（:${port}）: ${String(e.message).split('\n')[0]}`));

  const context = browser.contexts()[0] ?? await browser.newContext();
  // 全新 profile 首次启动会顺手开一堆 Edge 引导页，清掉（留最后一个，别把浏览器关了）
  if (started?.fresh) {
    const junk = /^(edge:\/\/|about:blank|https:\/\/www\.microsoft\.com|https:\/\/go\.microsoft\.com|https:\/\/nimbus)/;
    for (const p of context.pages()) {
      if (context.pages().length <= 1) break;
      if (junk.test(p.url())) await p.close().catch(() => {});
    }
  }

  await ensureLogin(context);
  return { browser, context, port };
}

// ───────────────────── 稿件列表：哪些日期已经有视频了 ─────────────────────
// 复投判定不看本地记录，看 B 站自己的稿件列表：某个日期已经排了定时发布（或已经发过），
// 就不该再投同一天的。字段语义（实测）：
//   · 定时发布中 → state=-40「通过审核，等待发布」，dtime=定时时刻，ptime/ctime=投稿时刻
//   · 已发布     → state=0「开放浏览」，ptime=实际发布时刻，dtime=null
// 所以「占用的日期」= state=-40 用 dtime，其余用 ptime（不能一律用 ptime：
// 定时中的稿件 ptime 是今天，会把今天误标成已占用）。
const BJ_DAY = (ts) => (ts > 0 ? new Date((ts + 8 * 3600) * 1000).toISOString().slice(0, 10) : null);

async function fetchScheduled(context) {
  const url = (pn) => 'https://member.bilibili.com/x/web/archives?status='
    + `${encodeURIComponent('is_pubing,not_pubed')}&pn=${pn}&ps=50&coop=&interactive=`;
  const headers = { Referer: 'https://member.bilibili.com/platform/upload-manager/article' };
  const days = new Map();
  try {
    for (let pn = 1; pn <= 3; pn++) { // 该接口按投稿时间倒序，未来日期的稿件一定在最前面几页
      const r = await context.request.get(url(pn), { headers, timeout: 30000 });
      if (!r.ok()) throw new Error(`HTTP ${r.status()}`);
      const j = await r.json();
      if (j.code !== 0) throw new Error(`code=${j.code} ${j.message ?? ''}`);
      const arcs = (j.data?.arc_audits ?? []).map((x) => x.Archive ?? {});
      if (!arcs.length) break;
      for (const a of arcs) {
        const ts = (a.state === -40 || (a.state !== 0 && a.dtime > 0)) ? a.dtime : (a.ptime || 0);
        const day = BJ_DAY(ts);
        if (day && !days.has(day)) {
          days.set(day, { title: String(a.title || ''), state: a.state, stateDesc: a.state_desc || a.stateDesc || '', bvid: a.bvid });
        }
      }
      if (pn * 50 >= (j.data?.page?.count ?? 0)) break;
    }
  } catch (e) {
    return { ok: false, error: e.message, days: new Map() };
  }
  return { ok: true, days };
}

// ───────────────────────── 页面工具 ─────────────────────────
// Playwright 的 setInputFiles 经 connectOverCDP 时按「远程浏览器」处理，
// 超过 50MB 会走传输通道直接报错。改用 CDP 原生 DOM.setFileInputFiles：
// 浏览器与脚本同机，直接传绝对路径让浏览器自己读，无大小限制。
async function setInputFilesViaCDP(page, selector, files) {
  const client = await page.context().newCDPSession(page);
  try {
    const { root } = await client.send('DOM.getDocument', { depth: -1, pierce: true });
    const { nodeId } = await client.send('DOM.querySelector', { nodeId: root.nodeId, selector });
    if (!nodeId) throw new Error(`CDP 未找到元素: ${selector}`);
    await client.send('DOM.setFileInputFiles', { files, nodeId });
  } finally {
    await client.detach().catch(() => {});
  }
}

async function gotoUploadPage(context, { fresh = false, newTab = false } = {}) {
  log(`[2/11] 打开投稿页`);
  let page = newTab ? null : context.pages().find((p) => p.url().includes('/platform/upload/video'));
  if (!page) page = await context.newPage();
  await page.bringToFront().catch(() => {});

  // 若页面已渲染出表单（说明已有视频在编辑中），不要重新加载 —— 刷新会丢已上传的视频
  // （批量模式下每条都用新标签页跑，fresh 让这条捷径失效，保证表单是干净的）
  const formReady = !fresh && page.url().includes('/platform/upload/video') && await page
    .evaluate(() => !!document.querySelector('input[placeholder="请输入稿件标题"]'))
    .catch(() => false);

  if (formReady) {
    log('      复用当前已渲染表单的页面（不重新加载，保留已上传视频）');
  } else {
    await page.goto(UPLOAD_PAGE, { waitUntil: 'domcontentloaded', timeout: 60000 });
    log(`      ${page.url()}`);
  }

  // 等上传区的 file input 就绪。页面上有 3 个 file input，真正被监听的是
  // .bcc-upload-wrapper 下那个隐藏 input（buploader 那两个是微前端干扰项）
  await page.waitForSelector('.bcc-upload-wrapper input[type=file]', { state: 'attached', timeout: 30000 });
  log('      上传区就绪 ✓');

  // 如果页面已有残留草稿，提示（避免误投）
  const hasDraft = await page.evaluate(() => {
    const el = [...document.querySelectorAll('.task-list-content-btn, [class*=file-item]')];
    return el.length > 0;
  });
  if (hasDraft) log('      (!) 页面已有上传中的视频文件，请注意是否为残留');
  return page;
}

async function setVideoFile(page, videoPath, { expectEmpty = false } = {}) {
  // 若页面已有上传中/上传完成的视频，跳过注入（便于分步调试与续跑）
  const existing = await page.evaluate(() => {
    const txt = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
    const task = [...document.querySelectorAll('.task-status, .task-list-content')].map(txt).filter(Boolean);
    const done = task.some((t) => /上传完成|上传中/.test(t));
    return { done, task: task.slice(0, 2) };
  });
  if (existing.done) {
    // 批量模式下页面本该是空的：这里冒出视频，就可能是上一个视频的残留 —— 继续填字段
    // 会把标题、封面、定时全按到那个视频上，投错稿。宁可跳过这条让人工看。
    if (expectEmpty) fail(`页面已有视频（${existing.task.join(' | ')}），批量模式下不继续，以免投错稿；请人工清空页面后重跑`);
    log(`[3/11] 页面已有视频，跳过注入（${existing.task.join(' | ')}）`);
    return;
  }

  const sizeMB = (statSync(videoPath).size / 1048576).toFixed(0);
  log(`[3/11] 注入视频: ${path.basename(videoPath)} (${sizeMB} MB)`);
  // 真正被监听的是 .bcc-upload-wrapper 下的隐藏 input
  await setInputFilesViaCDP(page, '.bcc-upload-wrapper input[type=file]', [videoPath]);
  log('      已注入，等待表单渲染…');
  // 注入超大视频会触发表单重建，标题字段可能延迟出现或 placeholder 有版本差异。
  // 轮询可见且可编辑的标题输入框；超时则输出当前页面状态便于定位，而非单 selector 黑盒超时。
  const titleSelector = 'input[placeholder="请输入稿件标题"]';
  try {
    await page.waitForFunction((selector) => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && !el.disabled;
    }, titleSelector, { timeout: 180000 });
  } catch (e) {
    const state = await page.evaluate(() => ({
      url: location.href, title: document.title,
      inputs: [...document.querySelectorAll('input')].map((el) => ({ placeholder: el.placeholder, type: el.type, visible: !!(el.offsetWidth || el.offsetHeight), disabled: el.disabled })).slice(0, 30),
      alerts: [...document.querySelectorAll('[role=alert], .upload-error, .error-msg')].map((el) => (el.innerText || '').trim()).filter(Boolean).slice(0, 10),
    })).catch(() => null);
    throw new Error(`注入视频后投稿表单未就绪（${JSON.stringify(state)}）；原始错误: ${e.message}`);
  }
  log('      表单已渲染 ✓');
  await sleep(2000);
}

async function setTitle(page, title) {
  log(`[4/11] 填写标题: ${title}`);
  const t = page.locator('input[placeholder="请输入稿件标题"]').first();
  await t.click({ timeout: 10000 });
  await t.fill('');
  await t.fill(title);
  const val = await t.inputValue();
  if (val !== title) fail(`标题写入不一致: "${val}"`);
  log('      ✓');
}

async function setCover(page, coverPath) {
  if (!coverPath) { log('[5/11] 跳过封面（目录下无可用封面图）'); return; }

  // 已设置过封面则跳过（.cover-empty-pill 是「添加封面」空态占位）
  const alreadySet = await page.evaluate(() => !document.querySelector('.cover-empty-pill'));
  if (alreadySet) {
    log('[5/11] 封面已设置，跳过');
    return;
  }

  log(`[5/11] 设置封面: ${path.basename(coverPath)}`);

  // 打开封面弹窗
  const pill = page.locator('.cover-empty-pill').first();
  if (await pill.count() && await pill.isVisible()) {
    await pill.click({ timeout: 10000 });
  } else {
    const coverSlot = page.locator('.cover-slot, .cover-main').first();
    await coverSlot.click({ timeout: 10000 });
  }
  await page.waitForSelector('.cover-editor', { timeout: 30000 });
  await sleep(2000);

  // 关键：先勾选「双比例同步改动」，否则 16:9 不会同步
  const syncResult = await page.evaluate(() => {
    const dlg = document.querySelector('.cover-editor');
    const cbs = [...dlg.querySelectorAll('.sync-checkbox-wrapper input[type=checkbox]')];
    const target = cbs.find((el) => el.getBoundingClientRect().width > 0) ?? cbs[0];
    if (!target) return 'no-checkbox';
    if (!target.checked) target.click();
    return `${target.checked}`;
  });
  log(`      双比例同步改动: ${syncResult}`);
  await sleep(1500);

  // 上传封面图
  await setInputFilesViaCDP(page, '.cover-editor input[type=file][accept*="image"]', [coverPath]);
  log('      已上传，等待同步…');
  await sleep(6000);

  // 校验两个比例都填上了
  const filled = await page.evaluate(() => {
    const dlg = document.querySelector('.cover-editor');
    const count = (label) => {
      const node = [...dlg.querySelectorAll('*')].find((el) => el.children.length === 0 && new RegExp(label).test(el.innerText || ''));
      if (!node) return -1;
      let box = node.parentElement;
      for (let i = 0; i < 4 && box?.parentElement; i++) box = box.parentElement;
      return box ? [...box.querySelectorAll('*')].filter((x) => getComputedStyle(x).backgroundImage.includes('url(')).length : -1;
    };
    return { r43: count('首页推荐封面'), r169: count('个人空间封面') };
  });
  log(`      封面填充: 4:3=${filled.r43} 处, 16:9=${filled.r169} 处`);

  // 点「完成」关闭弹窗（是 div.button.submit，不是 <button>）
  const done = page.locator('.cover-editor .cover-editor-button .button.submit').first();
  await done.click({ timeout: 15000 });
  await page.waitForSelector('.cover-editor', { state: 'detached', timeout: 30000 }).catch(() => {});
  await sleep(1500);
  log('      封面弹窗已关闭 ✓');
}

async function setStatement(page) {
  log(`[6/11] 创作声明: 内容无需标注 + ${STATEMENT_TEXT}`);
  const input = page.locator('input[placeholder="请选择符合您视频内容的创作声明"]').first();
  await input.scrollIntoViewIfNeeded().catch(() => {});

  const openDd = async () => {
    const open = await page.evaluate(() => {
      const isVis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      return [...document.querySelectorAll('.bcc-select-list-wrap')].some(isVis);
    });
    if (!open) {
      await input.click({ timeout: 15000 });
      await sleep(1500);
    }
  };
  await openDd();

  // ① 授权声明「内容为自制：未经作者允许，禁止转载」—— 这是个 toggle，
  //    选中标志是 .option-text 上的 option-text-selected 类。必须先确保它选中，
  //    因为点「内容无需标注」会把主选项切走，只有这个开关是独立的。
  const authState = () => page.evaluate(() => {
    const t = document.querySelector('.auth-content .option-text');
    return { exists: !!t, selected: t ? String(t.className).includes('option-text-selected') : false,
             inputVal: document.querySelector('input[placeholder="请选择符合您视频内容的创作声明"]')?.value };
  });
  let a = await authState();
  log(`      授权声明当前: ${a.selected ? '已选中' : '未选中'}`);
  if (!a.selected) {
    await page.evaluate(() => {
      const el = document.querySelector('.auth-content .option-text') || document.querySelector('.auth-content');
      el.click();
    });
    await sleep(1500);
    a = await authState();
    log(`      点击后: ${a.selected ? '已选中 ✓' : '(!) 仍未选中'}`);
  }

  // ② 主选项「内容无需标注」
  await openDd();
  const mainOk = await page.evaluate(() => {
    const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    for (const o of [...document.querySelectorAll('li.bcc-option')].filter(isVis)) {
      if ((o.innerText || '').trim() === '内容无需标注') {
        if (String(o.className).includes('selected')) return 'already';
        o.click();
        return 'clicked';
      }
    }
    return 'not-found';
  });
  if (mainOk === 'not-found') fail('未找到「内容无需标注」选项');
  await sleep(1500);

  // ③ 收起下拉并核对（注意：此时 input 显示的值是主选项，授权声明另有状态）
  // Escape 关不掉这个下拉，必须点空白处，否则浮层会挡住后续的分区/定时操作
  const ddClosed = await closeFloatingPanels(page);
  if (!ddClosed) log('      (!) 创作声明下拉未能关闭');
  await sleep(500);
  const fin = await page.evaluate(() => {
    const t = document.querySelector('.auth-content .option-text');
    return {
      main: document.querySelector('input[placeholder="请选择符合您视频内容的创作声明"]')?.value,
      authSelected: t ? String(t.className).includes('option-text-selected') : false,
      dropdownOpen: [...document.querySelectorAll('.bcc-select-list-wrap')].some((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }),
    };
  });
  log(`      主选项: "${fin.main}" | 授权声明(禁止转载): ${fin.authSelected ? '已选 ✓' : '未选 ✗'}`);
  if (!fin.authSelected) fail('授权声明未选中，请检查');
}

async function setPartition(page, name) {
  log(`[7/11] 分区: ${name}`);
  const controller = page.locator('.video-human-type .select-controller').first();
  await controller.scrollIntoViewIfNeeded().catch(() => {});
  await controller.click({ timeout: 15000 });
  await page.waitForTimeout(1800);

  const item = page.locator(`.human-type-list .drop-list-v2-item[title="${name}"]`).first();
  if (!(await item.count())) fail(`未找到分区: ${name}`);
  await item.click({ timeout: 15000 });
  await sleep(1500);

  const cur = await page.locator('.video-human-type .select-item-cont').first().innerText().catch(() => '');
  log(`      当前分区: "${cur.trim()}"`);
  if (cur.trim() !== name) log(`      (!) 分区值可能不符，期望 "${name}"`);
}

// 关闭浮层面板（创作声明下拉 / 日期日历 / 时间列）。
// 实测 Escape 对这些面板无效，必须点面板外的空白处 —— 面板是绝对定位浮层，
// 取它的几何位置往右偏移点击最稳。
const FLOATING_PANELS = [
  '.bcc-select-list-wrap',        // 创作声明等 bcc-select 下拉
  '.time-picker-panel-select-wrp', // 定时的小时/分钟列
  '.date-picker-container',       // 定时日历
];

async function closeFloatingPanels(page) {
  for (let i = 0; i < 5; i++) {
    const info = await page.evaluate((sels) => {
      const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
          if (!isVis(el)) continue;
          const r = el.getBoundingClientRect();
          return { sel, x: r.x + r.width, y: r.y + 20 };
        }
      }
      return null;
    }, FLOATING_PANELS);
    if (!info) return true; // 全部已关闭

    const vp = page.viewportSize() ?? { width: 1280 };
    const clickX = Math.min(info.x + 140, vp.width - 20);
    await page.mouse.click(clickX, info.y).catch(() => {});
    await sleep(700);
  }
  const stillOpen = await page.evaluate((sels) => {
    const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    return sels.some((sel) => [...document.querySelectorAll(sel)].some(isVis));
  }, FLOATING_PANELS);
  return !stillOpen;
}

async function setSchedule(page, dateStr, timeStr) {
  log(`[8/11] 定时发布: ${dateStr} ${timeStr}`);

  // 打开定时开关
  const sw = page.locator('.time-container .switch-container').first();
  await sw.scrollIntoViewIfNeeded().catch(() => {});
  const alreadyOn = await page.evaluate(() => {
    const s = document.querySelector('.time-container .switch-container');
    return !!s && String(s.className).includes('switch-container-active');
  });
  if (!alreadyOn) {
    await sw.click({ timeout: 15000 });
    await sleep(2000);
  }
  await page.waitForSelector('.time-picker', { timeout: 20000 });
  log('      定时开关已开启 ✓');

  const [yy, mm, dd] = dateStr.split('-').map(Number);
  const cur = await page.evaluate(() => {
    const tp = document.querySelector('.time-picker');
    return {
      date: tp?.querySelector('.date-picker-date .date-show')?.innerText?.trim(),
      time: tp?.querySelector('.date-picker-timer .date-show')?.innerText?.trim(),
    };
  });
  log(`      当前: ${cur.date} ${cur.time}`);

  // ── 选日期 ──
  if (cur.date !== dateStr) {
    log(`      选择日期 ${dateStr}…`);
    // 日历最多可往前选，先点开
    await page.locator('.time-picker .date-picker-date .date-show').first().click({ timeout: 10000 });
    await page.waitForSelector('.date-picker-container', { timeout: 10000 });
    await sleep(1200);

    // 导航到目标年月
    for (let i = 0; i < 36; i++) {
      const nav = await page.evaluate(() => document.querySelector('.date-picker-nav-title')?.innerText?.trim() || '');
      const m = nav.match(/(\d{4})年(\d{1,2})月/);
      if (!m) break;
      const cy = Number(m[1]), cm = Number(m[2]);
      if (cy === yy && cm === mm) break;
      const goPrev = (cy < yy) || (cy === yy && cm > mm);
      const sel = goPrev ? '.date-picker-nav-wrp .prev-btn-month' : '.date-picker-nav-wrp .next-btn-month';
      await page.locator(sel).first().click({ timeout: 8000 }).catch(() => {});
      await sleep(700);
    }
    const navTitle = await page.evaluate(() => document.querySelector('.date-picker-nav-title')?.innerText?.trim() || '');
    log(`      日历月份: ${navTitle}`);

    // 点目标日
    const picked = await page.evaluate((day) => {
      const isVis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const items = [...document.querySelectorAll('.date-picker-container .date-picker-body-item')].filter(isVis);
      for (const el of items) {
        if ((el.innerText || '').trim() === String(day) && !String(el.className).includes('disabled') && !String(el.className).includes('weekend')) {
          el.click();
          return 'clicked';
        }
      }
      return 'not-found';
    }, dd);
    if (picked === 'not-found') fail(`日历中未找到可选日期: ${dd}（可能超出 15 天窗口）`);
    await sleep(1500);
  }

  // ── 选时间 ──
  const curTime = await page.evaluate(() => document.querySelector('.time-picker .date-picker-timer .date-show')?.innerText?.trim());
  if (curTime !== timeStr) {
    log(`      选择时间 ${timeStr}…`);
    await page.locator('.time-picker .date-picker-timer .date-show').first().click({ timeout: 10000 });
    await page.waitForSelector('.time-picker-panel-select-wrp', { timeout: 10000 });
    await sleep(1200);

    const [hh, mi] = timeStr.split(':');
    const res = await page.evaluate(({ h, m }) => {
      const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const cols = [...document.querySelectorAll('.time-picker-panel-select-wrp')].filter(isVis);
      if (cols.length < 2) return 'cols<2';
      const pick = (col, val) => {
        const items = [...col.querySelectorAll('.time-picker-panel-select-item')];
        const t = items.find((el) => (el.innerText || '').trim() === val);
        if (!t) return 'not-found:' + val;
        if (String(t.className).includes('time-select-disabled')) return 'disabled:' + val;
        t.click();
        return 'ok';
      };
      const r1 = pick(cols[0], h);
      if (r1 !== 'ok') return 'hour-' + r1;
      return 'ok';
    }, { h: hh, m: mi });
    if (res !== 'ok') fail('小时选择失败: ' + res);
    await sleep(1200);

    // 选分钟（面板可能在小时间隔后重新渲染，重新取第 2 列）
    const res2 = await page.evaluate(({ m }) => {
      const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const cols = [...document.querySelectorAll('.time-picker-panel-select-wrp')].filter(isVis);
      const col = cols[cols.length - 1];
      const items = [...col.querySelectorAll('.time-picker-panel-select-item')];
      const t = items.find((el) => (el.innerText || '').trim() === m);
      if (!t) return 'not-found:' + m + ' (可用: ' + items.map((x) => (x.innerText || '').trim()).join(',') + ')';
      if (String(t.className).includes('time-select-disabled')) return 'disabled:' + m;
      t.click();
      return 'ok';
    }, { m: mi });
    if (res2 !== 'ok') fail('分钟选择失败: ' + res2);
    await sleep(1500);
  }

  // 收起面板并核对（Escape 无效，点空白处关闭）
  const closed = await closeFloatingPanels(page);
  if (!closed) log('      (!) 时间面板未能关闭，继续尝试读取');
  await sleep(500);
  const final = await page.evaluate(() => {
    const tp = document.querySelector('.time-picker');
    return {
      date: tp?.querySelector('.date-picker-date .date-show')?.innerText?.trim(),
      time: tp?.querySelector('.date-picker-timer .date-show')?.innerText?.trim(),
      tz: tp?.querySelector('.date-picker-timezone-wrp input')?.value,
    };
  });
  log(`      最终定时: ${final.date} ${final.time}  [${(final.tz || '').split(')')[0]})]`);
  if (final.date !== dateStr) log(`      (!) 日期期望 ${dateStr}，实际 ${final.date}`);
  if (final.time !== timeStr) log(`      (!) 时间期望 ${timeStr}，实际 ${final.time}`);
  return final;
}

// 可选步骤：参与话题。传入精确话题名则搜索并选中；未传入则整段跳过。
async function setTopic(page, topicName) {
  if (!topicName) { log('[9/11] 跳过话题（未传 --topic）'); return; }
  log(`[9/11] 参与话题: ${topicName}`);

  const dlgOpen = () => page.evaluate(() => {
    const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    return [...document.querySelectorAll('.bcc-dialog__wrap, .dialog')].some(isVis);
  });

  // 打开弹窗
  if (!(await dlgOpen())) {
    const more = page.locator('.tag-topic-wrp .tag-more').first();
    await more.scrollIntoViewIfNeeded().catch(() => {});
    await more.click({ timeout: 15000 });
    await sleep(2500);
  }
  if (!(await dlgOpen())) fail('话题弹窗未打开');

  // 搜索
  const si = page.locator('.bcc-dialog .bcc-search-input, .dialog .bcc-search-input').first();
  await si.click({ timeout: 15000 });
  await si.fill('');
  await si.type(topicName, { delay: 60 });
  await sleep(3000);

  // 精确匹配话题名（名字在 .topic-tag-name）
  // 注意：单选框是 toggle，已勾选时再点会取消 —— 所以先读状态，只在未勾选时点
  const hit = await page.evaluate((target) => {
    const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const items = [...document.querySelectorAll('.dialog-item')].filter(isVis);
    const names = items.map((it) => (it.querySelector('.topic-tag-name')?.innerText || '').trim());
    for (let i = 0; i < items.length; i++) {
      if (names[i] !== target) continue;
      const box = items[i].querySelector('.check-radio-v2-box');
      const alreadyChecked = !!box && String(box.className).includes('checked');
      if (!alreadyChecked) {
        const clickTarget = items[i].querySelector('.check-radio-v2-container') ?? items[i];
        clickTarget.click();
      }
      return { ok: true, name: names[i], alreadyChecked };
    }
    return { ok: false, names: names.slice(0, 15), total: names.length };
  }, topicName);

  if (!hit.ok) {
    // 搜不到直接报错退出（按需求）
    const hint = hit.total ? `可见 ${hit.total} 项，前几项: ${hit.names.join(' | ')}` : '列表为空';
    fail(`未搜索到话题「${topicName}」（${hint}）`);
  }
  log(`      ${hit.alreadyChecked ? '已是选中状态，跳过点击' : '已匹配并勾选'}: ${hit.name}`);
  await sleep(1200);

  // 校验单选框真的勾上了
  const checked = await page.evaluate((target) => {
    const isVis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    for (const it of [...document.querySelectorAll('.dialog-item')].filter(isVis)) {
      if ((it.querySelector('.topic-tag-name')?.innerText || '').trim() !== target) continue;
      const box = it.querySelector('.check-radio-v2-box');
      return !!box && String(box.className).includes('checked');
    }
    return false;
  }, topicName);
  if (!checked) fail('话题单选框未勾选成功');
  log('      单选框已勾选 ✓');

  // 点「确定」提交选择
  const confirm = page.locator('.bcc-dialog button.submit-add, .dialog button.submit-add').first();
  await confirm.click({ timeout: 15000 });
  await sleep(2500);

  if (await dlgOpen()) log('      (!) 弹窗未关闭');
  log('      话题已添加 ✓');
}

// 视频上传是异步的，上传没完成时「立即投稿」点了没有任何响应（0920 那次就是这样：
// 字段全填对了，提交却静默失败）。所以提交前必须等上传任务显示「上传完成」。
async function waitForUploadComplete(page, timeoutMin) {
  log(`[10/11] 等待视频上传完成（上限 ${timeoutMin} 分钟，--upload-wait 可调）…`);
  const t0 = Date.now();
  let last = '';
  for (;;) {
    const st = await page.evaluate(() => {
      const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const txt = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
      const tasks = [...document.querySelectorAll('.task-status, .task-list-content, [class*=task-item]')].filter(isVis).map(txt).filter(Boolean);
      const pct = ((document.body.innerText || '').match(/(\d{1,3})\s*%/) || [])[1];
      return {
        done: tasks.some((t) => /上传完成/.test(t)),
        failed: tasks.some((t) => /上传失败|重新上传/.test(t)),
        label: tasks.slice(0, 2).join(' | ') + (pct ? ` ${pct}%` : ''),
      };
    });
    if (st.failed) fail(`视频上传失败: ${st.label || '(页面提示上传失败)'}`);
    if (st.done) { log(`      上传完成 ✓（等待 ${Math.round((Date.now() - t0) / 1000)} 秒）`); return; }
    if (st.label !== last) { log(`      ${st.label || '上传中…'}`); last = st.label; }
    if (Date.now() - t0 > timeoutMin * 60_000) {
      fail(`等待视频上传完成超时（${timeoutMin} 分钟）。页面可能还在传，确认后再跑一次即可（已传完的不会重传）`);
    }
    await sleep(3000);
  }
}

// 返回 { status: 'dry' | 'ok' | 'failed' | 'unknown', why }
async function reportAndSubmit(page, { title, dateStr, timeStr, topic }, submit, shotName) {
  const summary = await page.evaluate(() => {
    const isVis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const txt = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
    const g = (sel) => { const el = document.querySelector(sel); return el ? txt(el).slice(0, 120) : null; };
    return {
      title: document.querySelector('input[placeholder="请输入稿件标题"]')?.value,
      statement: document.querySelector('input[placeholder="请选择符合您视频内容的创作声明"]')?.value,
      partition: g('.video-human-type .select-item-cont'),
      tags: [...document.querySelectorAll('.tag-container [class*=tag-content], .tag-container [class*=tag-item]')].filter(isVis).map((e) => txt(e)).slice(0, 12),
      // 参与话题最终是以「话题标签」形式落在标签区的 .label-item-v2-content 里
      topicTags: [...document.querySelectorAll('.tag-container .label-item-v2-content')].filter(isVis).map((e) => txt(e)),
      hasCover: !!document.querySelector('.cover-slot img, .cover-slot [class*=cover-img], .cover-empty-pill') ,
      coverPillGone: !document.querySelector('.cover-empty-pill'),
      schedule: {
        on: String(document.querySelector('.time-container .switch-container')?.className || '').includes('active'),
        date: document.querySelector('.time-picker .date-picker-date .date-show')?.innerText?.trim(),
        time: document.querySelector('.time-picker .date-picker-timer .date-show')?.innerText?.trim(),
      },
    };
  });

  log('\n═══════════ 投稿信息核对 ═══════════');
  log(`  标题    : ${summary.title}`);
  log(`  创作声明: ${summary.statement}`);
  log(`  分区    : ${summary.partition}`);
  log(`  标签    : ${(summary.tags || []).join(' / ')}`);
  if (topic) {
    const hit = (summary.topicTags || []).includes(topic);
    log(`  参与话题: ${topic} ${hit ? '✓' : '✗ 未找到！（话题可能未成功添加）'}`);
  } else {
    log(`  参与话题: (未指定)`);
  }
  log(`  封面    : ${summary.coverPillGone ? '已设置 ✓' : '(可能未设置)'}`);
  log(`  定时发布: ${summary.schedule.on ? '开' : '关'} ${summary.schedule.date || ''} ${summary.schedule.time || ''}`);
  log('═══════════════════════════════════\n');

  if (!submit) {
    log(`[11/11] 演练完成 —— 未提交。确认无误后加 --submit 重新运行。`);
    log(`      提示：当前页面已填好，可直接点「立即投稿」；或关闭页面重跑脚本。`);
    return { status: 'dry' };
  }

  log('[11/11] 提交…');
  const btn = page.locator('.submit-add:has-text("立即投稿"), .submit-container .submit-add').first();
  await btn.scrollIntoViewIfNeeded().catch(() => {});
  const urlBefore = page.url();
  await btn.click({ timeout: 15000 });
  log('      已点击「立即投稿」，观察结果…');

  // 轮询观察结果（不用 waitForURL：当前页 URL 本身就匹配 frame 路径，会立即误判）
  //   成功 → 跳离投稿页，或出现成功类提示
  //   失败 → 弹出错误弹窗（分区未选、标题违规等）
  let outcome = null;
  for (let i = 0; i < 45; i++) {
    await sleep(1000);
    const st = await page.evaluate(() => {
      const isVis = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const txt = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
      const dlgs = [...document.querySelectorAll('.bcc-dialog__wrap, .bcc-dialog')].filter(isVis).map(txt).filter(Boolean);
      const msgs = [...document.querySelectorAll('.bcc-message, [class*=message-success], [class*=message-error], .bcc-toast')].filter(isVis).map(txt).filter(Boolean);
      // 弹窗里是否有可点的确认按钮
      const confirmBtns = [...document.querySelectorAll('.bcc-dialog button, .bcc-dialog .button')].filter((b) => isVis(b) && /^(确定|确认|继续投稿|仍要投稿)$/.test(txt(b)));
      return { url: location.href, dlgs: dlgs.slice(0, 2), msgs: msgs.slice(0, 2), confirmCount: confirmBtns.length };
    });

    if (st.url !== urlBefore && !st.url.includes('/upload/video/frame')) {
      outcome = { ok: true, why: '已跳转到 ' + st.url };
      break;
    }

    const hint = [...st.dlgs, ...st.msgs].join(' | ');
    if (hint) {
      // 二次确认弹窗（如「确定要投稿吗」）→ 点掉继续等
      if (st.confirmCount > 0 && !/失败|错误|违规|不可|请选择|请填写/.test(hint)) {
        log(`      出现确认弹窗，点确认: ${hint.slice(0, 60)}`);
        await page.locator('.bcc-dialog button, .bcc-dialog .button').filter({ hasText: /^(确定|确认|继续投稿|仍要投稿)$/ }).first().click({ timeout: 8000 }).catch(() => {});
        await sleep(2000);
        continue;
      }
      const bad = /失败|错误|违规|不可|请选择|请填写|不能为空/.test(hint);
      outcome = { ok: !bad, why: hint };
      break;
    }
  }

  await page.screenshot({ path: shotName }).catch(() => {});
  if (!outcome) {
    log(`      结果: 45 秒内未见明确响应 —— 请在浏览器里人工确认（截图 ${shotName}）`);
    return { status: 'unknown', why: '45 秒内未见明确响应' };
  }
  if (outcome.ok) {
    log(`      投稿成功 ✓  ${outcome.why}`);
    return { status: 'ok', why: outcome.why };
  }
  log(`      投稿未成功 ✗  页面提示: ${outcome.why}`);
  log(`      截图: ${shotName}`);
  return { status: 'failed', why: outcome.why };
}

// ───────────────────────── 主流程 ─────────────────────────
if (ATTACH_ONLY) {
  await attach();
  log('\n--attach-only：只起/连浏览器，没有投稿动作。浏览器保持打开，下次直接复用。');
  process.exit(0);
}

// 一次投稿 = 一条流程。失败直接抛出，批量时由调用方记成这一条失败，不影响后面的目录
async function runOne(context, assets, { useNewTab }) {
  const page = await gotoUploadPage(context, { fresh: useNewTab, newTab: useNewTab });
  const shot = BATCH_SPEC ? `submit_result_${assets.dirName}.png` : 'submit_result.png';
  await setVideoFile(page, assets.videoPath, { expectEmpty: useNewTab });
  await setTitle(page, assets.title);
  await setCover(page, assets.coverPath);
  await setStatement(page);
  await setPartition(page, PARTITION);
  await setSchedule(page, assets.publishDate, TIME);
  await setTopic(page, TOPIC); // 可选，未传 --topic 则跳过
  await waitForUploadComplete(page, UPLOAD_WAIT_MIN);
  const outcome = await reportAndSubmit(
    page, { title: assets.title, dateStr: assets.publishDate, timeStr: TIME, topic: TOPIC }, SUBMIT, shot,
  );
  return { page, outcome };
}

// 结果落盘（给 pipeline.mjs 之类的外部调用方读，不解析控制台文本）
function writeResultJson(payload) {
  if (!RESULT_JSON) return;
  try {
    writeFileSync(path.resolve(RESULT_JSON), JSON.stringify(payload, null, 2) + '\n', 'utf8');
  } catch (e) {
    console.error(`✗ 写 ${RESULT_JSON} 失败: ${e.message}`);
  }
}

const targetJson = (t) => ({
  dir: t.dir, dirName: t.dirName, publishDate: t.publishDate, publishTime: TIME,
  title: t.title, videoPath: t.videoPath, videoMB: t.videoMB,
  coverPath: t.coverPath, coverKind: t.coverKind, warnings: t.warnings,
});

try {
  const plan = resolveTargets({ allowEmpty: PLAN });
  const tail = plan.skipped.length ? `，跳过 ${plan.skipped.length}` : '';
  const skippedLines = () => plan.skipped.map((s) => `- 跳过 ${s.name}：${s.why}`).forEach((l) => log(l));

  if (PLAN) {
    // --with-scheduled：顺手查一下 B 站上哪些日期已经排了稿件（pipeline.mjs 靠这个决定要不要生成封面）
    // scheduled = null 表示没查；{ ok:false } 表示查失败（调用方自己决定怎么办）
    let sched = null;
    let browserPort = null; // --with-scheduled 会真的连浏览器，把最终落在哪个端口带回去（调用方要防串端口）
    if (WITH_SCHEDULED) {
      const { context, port } = await attach();
      browserPort = port;
      log(`      查 B 站稿件列表（:${port}）…`);
      const r = await fetchScheduled(context);
      if (!r.ok) {
        sched = { ok: false, error: r.error };
        log(`      (!) 查询失败: ${r.error}`);
      } else {
        sched = { ok: true, days: Object.fromEntries(r.days) };
        const mine = plan.targets.filter((t) => r.days.has(t.publishDate));
        log(`      已有稿件的日期 ${r.days.size} 个；与本次目标重合 ${mine.length} 个`
          + (mine.length ? `：${mine.map((t) => `${t.dirName}=${t.publishDate}`).join(' ')}` : ''));
      }
    }

    if (JSON_OUT) {
      console.log(JSON.stringify({
        batch: plan.batch, root: plan.root, publishTime: TIME, topic: TOPIC,
        browserPort,
        targets: plan.targets.map(targetJson),
        skipped: plan.skipped,
        scheduled: sched,
      }, null, 2));
      process.exit(0);
    }

    log(`\n共 ${plan.targets.length} 个目标${plan.batch ? `（根目录 ${plan.root}）` : ''}${tail}：\n`);
    plan.targets.forEach((t, i) => { printTarget(t, i, plan.targets.length); log(''); });
    skippedLines();
    if (sched?.ok) {
      log('\nB 站已有稿件的日期（前 20 个）：');
      log(Object.keys(sched.days).sort().slice(0, 20)
        .map((d) => `  ${d}  ${sched.days[d].stateDesc}  ${String(sched.days[d].title).slice(0, 24)}`).join('\n'));
    }
    log('\n--plan 只列计划，没碰浏览器；去掉 --plan、加 --submit 才真投稿。');
    process.exit(0);
  }

  // 批量不加 --submit：只校验素材，不进浏览器。逐个把视频传上表单再丢掉，会白传几 G
  // 流量并在 B 站留一堆草稿 —— 要练流程用单目录跑一次就够了。
  if (plan.batch && !SUBMIT) {
    log(`\n共 ${plan.targets.length} 个目标（根目录 ${plan.root}）${tail} —— 只校验素材，未投稿：\n`);
    plan.targets.forEach((t, i) => { printTarget(t, i, plan.targets.length); log(''); });
    skippedLines();
    log('\n批量没加 --submit：没开浏览器、没注入视频（避免白传流量和一堆草稿）。');
    log(`真投稿：node bili_upload.mjs ${batchSpecArg()} --submit${TOPIC ? ` --topic "${TOPIC}"` : ''}`);
    writeResultJson({
      mode: 'batch', root: plan.root, submit: false, status: 'validated',
      results: plan.targets.map((t) => ({ ...targetJson(t), status: 'validated', why: null })),
      skipped: plan.skipped,
    });
    process.exit(0);
  }

  log(`共 ${plan.targets.length} 个目标${plan.batch ? `（根目录 ${plan.root}）` : ''}${tail}：`
    + plan.targets.map((t) => t.dirName).join(' '));
  skippedLines();
  log('');

  const { context } = await attach();

  // 复投判定：B 站上该日期已经有稿件的目录，直接跳过（不靠本地记录，避免换机器/删记录后重投）
  let targets = plan.targets;
  if (!FORCE) {
    const s = await fetchScheduled(context);
    if (!s.ok) {
      log(`      (!) 查 B 站稿件列表失败（${s.error}）—— 本轮只能按本地投稿记录去重，加 --force 可跳过在线检查`);
    } else {
      const taken = targets.filter((t) => s.days.has(t.publishDate));
      if (taken.length) {
        for (const t of taken) {
          const hit = s.days.get(t.publishDate);
          plan.skipped.push({ name: t.dirName, why: `${t.publishDate} 已有稿件（${hit.stateDesc}：${String(hit.title).slice(0, 30)}），要重投加 --force` });
        }
        targets = targets.filter((t) => !s.days.has(t.publishDate));
        log(`      在线去重：${taken.map((t) => t.dirName).join(' ')} 的日期已有稿件，跳过`);
      }
    }
  }

  const results = targets.map((t) => ({ ...t, status: 'pending' }));
  if (plan.batch) INDENT = '  '; // 批量时每条流程的日志缩进，好区分是哪一条

  for (const [i, t] of targets.entries()) {
    printTarget(t, i, targets.length);
    const r = results[i];
    try {
      // 批量一律用干净的新标签页跑：绝不复用可能残留着上一个视频的页面（否则会填错稿）
      const { page, outcome } = await runOne(context, t, { useNewTab: plan.batch });
      r.status = outcome.status === 'dry' ? 'ok' : outcome.status;
      r.why = outcome.why;
      // 只有确认成功才写记录、才关标签页；不确定的（unknown）留着让人工确认
      if (outcome.status === 'ok') {
        writeMarker(t, outcome.why);
        if (plan.batch) await page.close().catch(() => {});
      }
    } catch (e) {
      r.status = 'failed';
      r.why = e.message;
      log(`✗ ${t.dirName} 中断: ${e.message}`);
      if (!plan.batch) log('  浏览器保持打开，可人工检查页面状态后重跑。');
    }
    if (plan.batch) log('');
  }
  INDENT = '';

  const ok = results.filter((r) => r.status === 'ok');
  const unknown = results.filter((r) => r.status === 'unknown');
  const failed = results.filter((r) => r.status === 'failed');
  const bad = failed.length + unknown.length;

  if (!plan.batch) {
    writeResultJson({
      mode: 'single', submit: SUBMIT, status: bad ? 'failed' : 'ok',
      results: results.map((r) => ({ ...targetJson(r), status: r.status, why: r.why ?? null })),
      skipped: plan.skipped,
    });
    process.exit(bad ? 1 : 0);
  }

  log('════════════ 批量汇总 ════════════');
  for (const r of ok) log(`  ✓ ${r.dirName}  ${r.publishDate} ${TIME}${SUBMIT ? '' : '  (演练，未提交)'}`);
  for (const r of unknown) log(`  ! ${r.dirName}  提交结果未确认（${r.why}）—— 先去 B 站投稿管理看一眼，别重复投`);
  for (const r of failed) log(`  ✗ ${r.dirName}  ${r.why}`);
  for (const s of plan.skipped) log(`  - ${s.name}  跳过：${s.why}`);
  log(`\n成功 ${ok.length} / 共 ${targets.length}${plan.skipped.length ? `，跳过 ${plan.skipped.length}` : ''}`);
  if (unknown.length) log('有结果未确认的目录：先在 B 站确认，再决定要不要加 --force 重投');
  else if (failed.length) log('失败/中断的目录直接重跑同一条命令即可续跑（投过的会被记录跳过）');

  writeResultJson({
    mode: 'batch', root: plan.root, submit: SUBMIT, status: bad ? 'failed' : 'ok',
    results: results.map((r) => ({ ...targetJson(r), status: r.status, why: r.why ?? null })),
    skipped: plan.skipped,
  });

  // connect 模式不关闭用户浏览器
  process.exit(bad ? 2 : 0);
} catch (e) {
  console.error('\n✗ ' + (e?.message ?? e));
  writeResultJson({ mode: BATCH_SPEC ? 'batch' : 'single', status: 'error', error: String(e?.message ?? e), results: [], skipped: [] });
  process.exit(1);
}
