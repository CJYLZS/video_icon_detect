#!/usr/bin/env node
/**
 * pipeline.mjs —— 一条命令跑完「补封面 → 投稿」的流水线
 *
 * 两个阶段，各自独立的浏览器 session，绝不共用调试端口：
 *   阶段1 封面：目录里没有美化封面（封面_chatgpt.png / <uuid>.png）时，调 chatgpt_cover.mjs 生成。
 *               它用 cg_profiles.json 里的账号和端口。
 *   阶段2 上传：调 bili_upload.mjs 投稿，用 B 站自动化专用 profile，默认端口 9235（--bili-port 可改）。
 *   两个工具都按「端口占用者」认自己的浏览器（msedge + 自己的 --user-data-dir / --profile-directory）：
 *   端口上如果是别人的浏览器（chatgpt 的、系统里的 Chrome、别的工具），直接报错退出、绝不复用 ——
 *   否则就会出现「拿 bili 的 profile 开 ChatGPT，页面显示没登录」这种事。
 *
 * 复投判定不靠本地记录：查 B 站稿件列表，目标日期上已经有稿件（定时发布中或已发布）就跳过，
 * 日志里写明撞的是哪一条。查不到稿件列表时默认拒绝执行阶段2，加 --force 才硬来。
 * （--plan 也会连一下 bili 浏览器读稿件列表：没开着就顺手起来，只读，不投稿）
 *
 * 用法：
 *   node pipeline.mjs --root "F:/video_edit/final_backup/2026.10" --dirs 1001-1010 --plan
 *   node pipeline.mjs --root "F:/video_edit/final_backup/2026.10" --dirs 1001-1010 --submit
 *   node pipeline.mjs "F:/video_edit/final_backup/2026.10" --dirs 1001-1010 --submit --topic 和平精英激战金秋
 *   node pipeline.mjs --root "F:/..." --all --submit
 *   node pipeline.mjs --root "F:/..." --dirs 1004 --cover-only               # 只补封面
 *   node pipeline.mjs --root "F:/..." --dirs 1004 --upload-only --submit     # 只上传
 *
 * 选项：
 *   --root <路径>        素材根目录（也可直接当位置参数写）
 *   --dirs <列表|范围>   一级目录名，如 1001,1003 或 1001-1010；--all 表示根目录下全部
 *   --submit             阶段2 真投稿（不加只做素材校验，不会白传视频）
 *   --plan               只打印两阶段的计划（含要跑的命令），不执行
 *   --cover-only         只跑阶段1；--upload-only 只跑阶段2
 *   --allow-raw-cover    封面生成失败的目录也用原始截图 封面.png 投（默认跳过等人工）
 *   --force              不跳过已投稿的目录（重投）+ 稿件列表查不到也硬跑
 *   --profile <名字>     阶段1 指定 ChatGPT 账号（转发给 chatgpt_cover.mjs）
 *   --bili-port <n>      阶段2 的 CDP 端口（默认 9235）
 *   --topic / --time / --upload-wait / --login-wait   转发给 bili_upload.mjs
 *
 * 退出码：0 = 全部符合预期；2 = 有失败/结果未确认；1 = 参数或前置条件不对
 */
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync, unlinkSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_DIR = path.resolve(__dirname, '..');
const BILI = path.join(__dirname, 'bili_upload.mjs');
const COVER = path.join(__dirname, 'chatgpt_cover.mjs');
const CG_PROFILES = path.join(WORKSPACE_DIR, 'cg_profiles.json');
const CG_PROFILES_EXAMPLE = path.join(WORKSPACE_DIR, 'config', 'cg_profiles.example.json');
const DEFAULT_BILI_PORT = 9235; // 避开 cg_profiles.json 所有 profile 的端口，也避开 Windows 保留段（9238-9337 等）

const args = process.argv.slice(2);
const has = (f) => args.includes(f);
const val = (f, d) => {
  const i = args.indexOf(f);
  return i >= 0 && args[i + 1] && !args[i + 1].startsWith('--') ? args[i + 1] : d;
};
const log = (...a) => console.log(...a);
const die = (m) => { console.error('✗ ' + m); process.exit(1); };

const PLAN = has('--plan');
const SUBMIT = has('--submit');
const COVER_ONLY = has('--cover-only');
const UPLOAD_ONLY = has('--upload-only');
const FORCE = has('--force');
const ALLOW_RAW = has('--allow-raw-cover');
const ROOT = val('--root', null);
const DIRS = val('--dirs', null);
const TOPIC = val('--topic', null);
const TIME = val('--time', null);
const PROFILE = val('--profile', null);
const BILI_PORT = Number(val('--bili-port', String(DEFAULT_BILI_PORT)));
const UPLOAD_WAIT = val('--upload-wait', null);
const LOGIN_WAIT = val('--login-wait', null);

const VALUE_OPTS = ['--root', '--dirs', '--topic', '--time', '--profile', '--bili-port', '--upload-wait', '--login-wait'];
const optIdx = new Set(VALUE_OPTS.map((f) => args.indexOf(f) + 1).filter((i) => i > 0));
const POS = args.find((a, i) => !a.startsWith('--') && !optIdx.has(i));
const ROOT_ARG = ROOT ?? POS;

if (!ROOT_ARG) {
  console.error('用法: node pipeline.mjs --root <根目录> --dirs 1001-1010 [--submit] [--topic 话题名] [--plan]');
  console.error('  例: node pipeline.mjs --root "F:/video_edit/final_backup/2026.10" --dirs 1001-1010 --plan');
  console.error('  例: node pipeline.mjs "F:/video_edit/final_backup/2026.10" --all --submit');
  process.exit(1);
}
if (!DIRS && !has('--all')) die('需要 --dirs <目录名列表|1001-1010> 或 --all');
if (COVER_ONLY && UPLOAD_ONLY) die('--cover-only 和 --upload-only 只能选一个');
if (!Number.isFinite(BILI_PORT)) die(`--bili-port 不是数字: ${BILI_PORT}`);

// ── 两个阶段的端口不能撞：bili 的端口必须避开 chatgpt 所有 profile 的端口。
//    enabled:false 的也算 —— 它照样能被 --profile 显式选中，选中后就会去抢那个端口
// ──
function cgAccounts() {
  try {
    const cfg = JSON.parse(readFileSync(CG_PROFILES, 'utf8'));
    return (cfg.profiles ?? []).map((p, i) => ({
      name: p.name, port: p.port ?? 9222 + i, enabled: p.enabled !== false,
    }));
  } catch { return []; }
}
const accounts = cgAccounts();
const clash = accounts.find((p) => p.port === BILI_PORT);
if (clash) {
  die(`--bili-port ${BILI_PORT} 和生图 profile ${clash.name} 的端口撞了`
    + `${clash.enabled ? '' : `（它虽然默认不启用，但 --profile ${clash.name} 能显式选中）`}。\n`
    + '  两个阶段的浏览器必须是两个 session，不能共用一个调试端口，换个端口再跑（如 --bili-port ' + (BILI_PORT + 1) + '）');
}

// ── 跑子进程：正常情况直接继承 stdio，让人看到两个工具自己的输出 ──
function runNode(script, argv, { quiet = false } = {}) {
  return new Promise((resolve) => {
    log(`      → node ${path.basename(script)} ${argv.join(' ')}`);
    const child = spawn(process.execPath, [script, ...argv], {
      stdio: quiet ? ['ignore', 'pipe', 'pipe'] : 'inherit', windowsHide: false,
    });
    let out = '', err = '';
    if (quiet) {
      child.stdout.on('data', (d) => { out += d; });
      child.stderr.on('data', (d) => { err += d; });
    }
    child.on('error', (e) => resolve({ code: -1, out, err: String(e.message) }));
    child.on('close', (code) => resolve({ code, out, err }));
  });
}

// ChatGPT 阶段只检查美化产物；人工定稿的 封面.jpg 已由 bili_upload 识别，无需再次生成
const UUID_PNG = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.png$/i;
const polishedCover = (dir) => {
  try {
    const files = readdirSync(dir);
    const polished = files.filter((n) => UUID_PNG.test(n) || n === '封面_chatgpt.png');
    const f = polished
      .map((name) => ({ name, mtime: statSync(path.join(dir, name)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime)[0]?.name;
    return f ? path.join(dir, f) : null;
  } catch { return null; }
};

// ───────────────────── ① 读计划（目录 / 日期 / 封面 / 已占用日期）─────────────────────
const targetArgs = DIRS ? ['--dirs', DIRS] : ['--all'];
log(`[0/2] 取计划：目录素材 + B 站稿件列表（连 :${BILI_PORT} 只读，不投稿）`);
const planArgs = ['--root', ROOT_ARG, ...targetArgs, '--plan', '--json', '--with-scheduled', '--port', String(BILI_PORT)];
if (TIME) planArgs.push('--time', TIME);
const pr = await runNode(BILI, planArgs, { quiet: true });
let plan = null;
try { plan = JSON.parse(pr.out); } catch { /* 下面统一报错 */ }
if (!plan) {
  console.error(pr.err || pr.out || '');
  die(`取计划失败（退出码 ${pr.code}）`);
}
// 子进程的诊断日志（走的 stderr）：端口落在哪、登录态、稿件列表查得怎么样
for (const l of pr.err.split('\n').filter((l) => l.trim())) log(`      | ${l.trim()}`);

const schedDays = plan.scheduled?.ok ? plan.scheduled.days : {};
const schedErr = plan.scheduled && !plan.scheduled.ok ? plan.scheduled.error : null;
if (schedErr) log(`      (!) 稿件列表没查到: ${schedErr}`);

// bili 的浏览器可能上一轮就开着（bili_upload 会按 profile 复用），万一它现在正跑在生图账号的
// 端口上，阶段1 一起来就串 session —— 这里提前拦下来，把要关的东西说清楚
if (plan.browserPort && accounts.some((p) => p.port === plan.browserPort)) {
  const c = accounts.find((p) => p.port === plan.browserPort);
  die(`bili 的自动化 Edge 现在跑在 :${plan.browserPort}，正好是生图 profile ${c.name} 的端口 —— 两个 session 不能共用端口。\n`
    + '  把那个自动化 Edge 窗口关掉再重跑（下次它会听 --bili-port 的端口）；或改 cg_profiles.json 里这个 profile 的 port。');
}

// ── 分类：已投稿 / 要生成封面 / 可上传 ──
const items = plan.targets.map((t) => {
  const hit = schedDays[t.publishDate] ?? null;
  const taken = hit && !FORCE;
  const coverOk = t.coverKind === 'polished';
  return {
    ...t,
    hit, taken,
    coverStage: !taken && !UPLOAD_ONLY && !coverOk && t.coverKind !== null, // 有原始截图才谈得上生成
    coverResult: taken ? '不处理' : coverOk ? '已有' : t.coverKind === null ? '无原始截图' : '待生成',
    uploadResult: taken ? '不处理' : coverOk || ALLOW_RAW ? '待投稿' : '等封面',
  };
});

const needCover = items.filter((i) => i.coverStage);
// 现在就能投的（已有封面）+ 阶段1 补完封面后能投的（有原始截图可生成）+ --allow-raw-cover 兜底
const uploadDirs = items
  .filter((i) => !i.taken && (i.coverResult === '已有' || i.coverStage || ALLOW_RAW))
  .map((i) => i.dirName);
const stuck = items.filter((i) => !i.taken && i.coverResult === '无原始截图' && !ALLOW_RAW);

const coverCmd = ['--root', ROOT_ARG, '--dirs', needCover.map((i) => i.dirName).join(',')];
if (PROFILE) coverCmd.push('--profile', PROFILE);
const uploadCmd = ['--root', ROOT_ARG, '--dirs', uploadDirs.join(','), '--port', String(BILI_PORT)];
if (SUBMIT) uploadCmd.push('--submit');
if (FORCE) uploadCmd.push('--force');
if (TOPIC) uploadCmd.push('--topic', TOPIC);
if (TIME) uploadCmd.push('--time', TIME);
if (UPLOAD_WAIT) uploadCmd.push('--upload-wait', UPLOAD_WAIT);
if (LOGIN_WAIT) uploadCmd.push('--login-wait', LOGIN_WAIT);
const RESULT_JSON = path.join(os.tmpdir(), `bili_pipeline_${process.pid}.json`);
uploadCmd.push('--result-json', RESULT_JSON);

// ───────────────────── ② 计划模式：只打印要做的事 ─────────────────────
if (PLAN) {
  log(`\n共 ${items.length} 个目录（根目录 ${plan.root}，${plan.publishTime} 发布）：`);
  for (const i of items) {
    const mark = i.taken ? '-' : i.coverStage ? '+' : i.uploadResult.startsWith('等') ? '!' : '✓';
    log(`  ${mark} ${i.dirName}  ${i.publishDate}  封面:${i.coverResult}  上传:${i.uploadResult}  ${String(i.title).slice(0, 26)}`);
    if (i.taken) log(`      跳过：该日期已有稿件（${i.hit.stateDesc}：${String(i.hit.title).slice(0, 30)}）`);
  }
  for (const s of plan.skipped) log(`  - ${s.name}  跳过：${s.why}`);
  if (stuck.length) log(`  ! ${stuck.map((i) => i.dirName).join(' ')} 没有可用原始封面（封面.png），无法生成美化封面（用 --allow-raw-cover 可硬投）`);
  if (needCover.length && !UPLOAD_ONLY) log(`\n阶段1 要生成封面 ${needCover.length} 个：${needCover.map((i) => i.dirName).join(' ')}`);
  else log('\n阶段1 没有要生成的封面');
  log(`阶段2 要投稿 ${uploadDirs.length} 个：${uploadDirs.join(' ') || '(无)'}`);
  if (needCover.length && !UPLOAD_ONLY) log(`\n阶段1 命令: node chatgpt_cover.mjs ${coverCmd.join(' ')}`);
  log(uploadDirs.length
    ? `阶段2 命令: node bili_upload.mjs ${uploadCmd.slice(0, -2).join(' ')}${SUBMIT ? '' : '   # 没加 --submit，只校验素材'}`
    : '阶段2 没有要投稿的目录');
  log('\n--plan 只是列计划。真跑就去掉 --plan' + (SUBMIT ? '' : '、加上 --submit'));
  process.exit(0);
}

// 稿件列表查不到时，宁可不动阶段2（会重复投稿），除非 --force
if (schedErr && !FORCE && !COVER_ONLY) {
  die(`查不到 B 站稿件列表（${schedErr}），无法判断哪些日期已经投过。\n`
    + '  想照常投就加 --force（不跳过任何目录），或先确认 bili 浏览器登录态后重跑。');
}

// ───────────────────── ③ 阶段1：生成封面 ─────────────────────
let coverFailed = [];
if (!UPLOAD_ONLY && needCover.length) {
  log(`\n[1/2] 生成封面：${needCover.length} 个目录（${needCover.map((i) => i.dirName).join(' ')}）`);
  const r = await runNode(COVER, coverCmd);
  for (const i of needCover) {
    const f = polishedCover(i.dir);
    i.coverResult = f ? '新生成' : '生成失败';
    i.coverPath = f ?? i.coverPath;
    if (!f) coverFailed.push(i);
  }
  log(`      阶段1 结束（退出码 ${r.code}）：生成成功 ${needCover.length - coverFailed.length}，失败 ${coverFailed.length}`);
} else if (UPLOAD_ONLY) {
  log('\n[1/2] --upload-only：跳过生成封面');
} else {
  log('\n[1/2] 没有需要生成的封面，跳过');
}

// 定稿每条的投稿资格：封面到位才投；没到位默认不投（免得把原始截图/没封面投上去），
// --allow-raw-cover 才放行。--upload-only 下没生成的也走这里，同样不放行。
for (const i of items) {
  if (i.taken) continue;
  if (['已有', '新生成'].includes(i.coverResult) || ALLOW_RAW) {
    i.uploadResult = '待投稿';
  } else {
    i.uploadResult = i.coverResult === '无原始截图' ? '等封面（无原始截图）'
      : i.coverResult === '待生成' ? '等封面（--upload-only 没生成）' : '等封面（生成失败）';
  }
}

// ───────────────────── ④ 阶段2：投稿 ─────────────────────
const finalUpload = items.filter((i) => i.uploadResult === '待投稿');
if (COVER_ONLY) {
  log('\n[2/2] --cover-only：跳过投稿');
} else if (!finalUpload.length) {
  log('\n[2/2] 没有需要投稿的目录，跳过');
} else {
  log(`\n[2/2] 投稿：${finalUpload.length} 个目录（${finalUpload.map((i) => i.dirName).join(' ')}）`
    + (SUBMIT ? '' : '（没加 --submit，只做素材校验）'));
  uploadCmd[uploadCmd.indexOf('--dirs') + 1] = finalUpload.map((i) => i.dirName).join(',');
  const r = await runNode(BILI, uploadCmd);
  let res = null;
  try { res = JSON.parse(readFileSync(RESULT_JSON, 'utf8')); } catch { /* 见下 */ }
  try { unlinkSync(RESULT_JSON); } catch { /* 清不掉就算了 */ }
  if (res) {
    for (const r of res.results) {
      const it = items.find((i) => i.dirName === r.dirName);
      if (!it) continue;
      it.uploadResult = {
        ok: SUBMIT ? '成功' : '素材OK（未投稿）',
        unknown: '结果未确认', failed: '失败', validated: '素材OK（未投稿）',
      }[r.status] ?? r.status;
      if (r.why && r.status !== 'ok') it.uploadWhy = r.why;
    }
    for (const s of res.skipped) {
      const it = items.find((i) => i.dirName === s.name);
      if (it && !it.taken) it.uploadResult = `跳过（${s.why}）`;
    }
  } else {
    log(`      (!) 没读到 bili_upload 的结果 JSON（退出码 ${r.code}）`);
    for (const i of finalUpload) i.uploadResult = `未确认（退出码 ${r.code}）`;
  }
}

// ───────────────────── ⑤ 汇总 ─────────────────────
log('\n════════════ 流水线汇总 ════════════');
let bad = 0;
const OK_UP = ['成功', '素材OK（未投稿）', '不处理'];
for (const i of items) {
  const up = i.uploadResult;
  const okUp = OK_UP.includes(up);
  if (!i.taken && !okUp) bad++;
  const mark = i.taken || up.startsWith('等') || up.startsWith('跳过') ? '-' : okUp ? '✓' : '✗';
  log(`  ${mark} ${i.dirName}  ${i.publishDate}  封面:${i.coverResult}  上传:${up}${i.uploadWhy ? `（${i.uploadWhy}）` : ''}`);
}
for (const s of plan.skipped) log(`  - ${s.name}  跳过：${s.why}`);
const done = items.filter((i) => ['成功', '素材OK（未投稿）'].includes(i.uploadResult)).length;
log(`\n投稿完成 ${done} / 共 ${items.length}${plan.skipped.length ? `，素材不齐 ${plan.skipped.length}` : ''}`);
if (bad) log('有目录卡在失败/未确认：按上面的提示处理，重跑同一条命令即可续跑（投过的会被跳过）');

process.exit(bad ? 2 : 0);
