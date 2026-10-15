#!/usr/bin/env node
/**
 * chatgpt_cover.mjs —— 用 ChatGPT 网页版把游戏截图改成视频封面
 *
 * 浏览器复用模式（重要）：
 *   · 浏览器以 --remote-debugging-port 独立启动，脚本退出后它继续活着
 *   · 再次运行时先探测 CDP 端口：浏览器/标签页还在就直接复用，不再重启
 *   · 复用只认「自己的」浏览器：端口上必须是 msedge + 本账号的 --user-data-dir / --profile-directory。
 *     端口被别的程序（系统里的 Chrome、bili_upload 的自动化 Edge、别的账号）占着就直接报错，
 *     绝不复用 —— 否则会把 ChatGPT 开进别人的 session，看起来就像「没登录」。
 *   · 默认退出时【不清理】浏览器和标签页（方便反复调试），加 --cleanup 才关标签页并杀浏览器
 *
 * 流程：
 *   1. 复用或启动该账号的浏览器 + ChatGPT 标签页（复用已登录会话）
 *   2. 新会话里上传 封面.png，填入美化提示词
 *   3. 等生成完成，把图下载到本地
 *   4. 按目标比例（默认 4:3）居中裁剪，另存为最终封面
 *   5. 额度耗尽 → 记入工作区根目录 cg_state.json，自动切下一个 profile 重试
 *
 * 用法：
 *   node scripts/chatgpt_cover.mjs 0920             # 处理 <工作区根目录>/0920/封面.png
 *   node chatgpt_cover.mjs "F:/video_edit/final_backup/0920/封面.png"
 *   node chatgpt_cover.mjs 0920 --dry               # 只上传+填提示词，停在发送前（调试）
 *   node chatgpt_cover.mjs 0920 --as-cover          # 输出成 bili_upload 认得的 uuid.png
 *   node chatgpt_cover.mjs --login                  # 依次打开各 profile 等待手动登录
 *   node chatgpt_cover.mjs --probe                  # 周期 dump DOM（调选择器用）
 *   node chatgpt_cover.mjs --root F:/video_edit/final_backup --dirs 0920,0921   # 批量：根目录 + 一级目录名
 *   node chatgpt_cover.mjs --root F:/video_edit/final_backup --dirs 1004-1010  # 批量：连续编号简写
 *   node chatgpt_cover.mjs --root F:/video_edit/final_backup --all             # 批量：根目录下所有含 封面.png 的一级目录
 *   node chatgpt_cover.mjs --root F:/... --dirs 0920,0921 --plan              # 只列出将要处理的目标，不生成
 *   node chatgpt_cover.mjs --list                   # 看 profile / 端口 / 额度状态
 *   node chatgpt_cover.mjs 0920 --harvest           # 不重发，直接抓当前页面上已生成的图
 *   node chatgpt_cover.mjs --close-only             # 只关掉浏览器，不生成
 *
 * 常用选项：
 *   --profile <名字|序号>   只用指定 profile（逗号分隔或重复写）
 *   --port <n>              覆盖 CDP 端口
 *   --prompt "..." / --prompt-file <路径>
 *   --out <路径>            指定最终输出文件
 *   --no-fit / --fit 16:9 / --fit-mode pad
 *   --timeout 15            等待生图分钟数（免费版出图慢，默认给足）
 *   --login-timeout 15      等待登录分钟数；0 = 未登录就直接退出不等待
 *   --keep-page             不重载标签页，直接用当前页面（调试用）
 *   --headless（调试用不上，CDP 模式下仅作记录）
 *   --debug / --keep / --force / --cleanup
 */
import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_DIR = path.resolve(__dirname, '..');

// ───────────────────────── 配置 ─────────────────────────
const SITE = 'https://chatgpt.com/';
const DEFAULT_PROMPT =
  '将这个大战场游戏截图变成视频封面，去除所有的操作按键，增加艺术效果，爆炸效果，冲击感，不要增加任何文字，图像比例是4:3';

const PROFILES_FILE = path.join(WORKSPACE_DIR, 'cg_profiles.json');
const PROFILES_EXAMPLE_FILE = path.join(WORKSPACE_DIR, 'config', 'cg_profiles.example.json');
const STATE_FILE = path.join(WORKSPACE_DIR, 'cg_state.json');
const DEBUG_DIR = path.join(WORKSPACE_DIR, 'cg_debug');
const DEFAULT_PORT = 9231; // 只在 profile 没写 port 时兜底用；避开 9222/9223（常被无头 Chrome 占）和 Windows 保留段 9238+

// 选择器集中在这里，ChatGPT 改版时优先改这一块
const SEL = {
  editor: ['#prompt-textarea', 'div[contenteditable="true"].ProseMirror', 'div[contenteditable="true"]', 'textarea'],
  sendButton: [
    'button[data-testid="send-button"]',
    '#composer-submit-button',
    'button[aria-label="发送消息"]',
    'button[aria-label*="发送"]',
    'button[aria-label*="Send"]',
  ],
  stopButton: [
    'button[data-testid="stop-button"]',
    '#composer-submit-button[aria-label*="停止"]',
    'button[aria-label*="停止生成"]',
    'button[aria-label*="Stop"]',
  ],
  assistantTurn: ['[data-message-author-role="assistant"]', 'article[data-testid^="conversation-turn"]'],
  loginButton: ['[data-testid="login-button"]'],
  downloadButton: [
    'button[aria-label*="下载"]',
    'button[aria-label*="Download"]',
    'button[title*="下载"]',
    'button[title*="Download"]',
    '[data-testid*="download"]',
  ],
  dialog: ['[role="dialog"]', '[role="alertdialog"]', '[role="alert"]'],
  // 上传中：缩略图上层盖着进度环（带 stroke-dasharray 的 circle），外层容器带 cursor-wait。
  // 上传完成后这两个信号同时消失（实测 6MB 图上传约 9 秒），必须等它结束再发送
  uploadBusy: ['form circle[stroke-dasharray]', 'form .cursor-wait'],
  // 生成结果图的定位：当前 UI 把出图放在 div[class*=imagegen-image] 容器里，
  // 位置上是 assistant 消息的兄弟节点，所以不能只扫 assistant 回合
  genImg: [
    'div[class*="imagegen-image"] img',
    'div[id^="image-"] img',
    'img[alt^="已生成图片"]',
    'img[alt^="已生成图像"]',
    'button[data-testid="generated-image-preview"] img',
    '[data-testid="generated-image-gallery"] img',
    'img[alt^="Generated image"]',
  ],
  // 只放「生成中」独有的信号；不要放 [class*=imagegen]，那个类名生成完也不会消失
  genPlaceholder: [
    '[data-testid*="image-generation"]',
    '[aria-label*="正在生成图片"]',
    'img[alt*="正在生成"]',
    'img[alt*="Generating"]',
  ],
};

// 额度耗尽 / 套餐不支持 / 明确拒绝 的文本特征（中英双语）
const PAT = {
  quota: [
    /(图片|图像|照片)[^。！\n]{0,18}(上限|限额|额度|次数|配额)/,
    /(上限|限额|额度|配额|次数)[^。！\n]{0,18}(图片|图像|照片)/,
    /(达到|已达|用完|耗尽)[^。！\n]{0,12}(上限|限额|额度|次数)/,
    /今日[^。！\n]{0,10}(已用完|次数)/,
    /(image|images)[^.\n]{0,20}(generation|generations|gen)[^.\n]{0,20}(limit|cap)/i,
    /(limit|cap)[^.\n]{0,20}(image|images)/i,
    /reached (the|your) limit/i,
    /you'?ve (hit|reached)[^.\n]{0,30}(limit|cap)/i,
    /out of (image )?(generations|credits)/i,
    /limit resets/i,
  ],
  unavailable: [
    /(需要|要求|仅)[^。！\n]{0,12}(Plus|Pro|会员|订阅|付费)/,
    /(upgrade|subscribe)[^.\n]{0,20}(Plus|Pro)/i,
    /(isn'?t|is not) available[^.\n]{0,30}(free|plan)/i,
  ],
  refusal: [
    /(无法|不能|没法)[^。！\n]{0,16}(生成|编辑|修改|创建|处理)[^。！\n]{0,8}(图|图像|图片|照片)/,
    /(can'?t|cannot|unable to|am not able to)[^.\n]{0,30}(generate|edit|create|modify|process)[^.\n]{0,20}(image|picture|photo)/i,
  ],
  // 服务端出图失败（通常可重试）
  genError: [
    /(图片|图像)[^。！\n]{0,10}(生成|创建)[^。！\n]{0,10}(失败|出错|错误)/,
    /(生成|创建)[^。！\n]{0,6}(图片|图像)[^。！\n]{0,10}(失败|出错)/,
    /(出错了|发生错误|出现错误)/,
    /something went wrong/i,
    /(failed to generate|error generating|generation failed)/i,
  ],
};

// ───────────────────────── 小工具 ─────────────────────────
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const nowStr = () => new Date().toTimeString().slice(0, 8);
const expand = (p) => path.resolve(String(p).replace(/%([^%]+)%/g, (_, k) => process.env[k] ?? ''));
const ensureDir = (d) => { if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true }); };
function log(tag, ...a) { console.log(`[${nowStr()}]${tag ? `[${tag}]` : ''}`, ...a); }
function die(m) { console.error(`\n✗ ${m}\n`); process.exit(1); }

// ───────────────────────── 命令行 ─────────────────────────
function parseArgs(argv) {
  const o = {
    input: null, profileSel: null, prompt: DEFAULT_PROMPT, out: null,
    fit: true, fitRatio: 4 / 3, fitMode: 'crop', timeoutMin: 15, loginTimeoutMin: 15,
    debug: false, keep: false, dry: false, probe: false, login: false,
    force: false, list: false, asCover: false, cleanup: false, closeOnly: false,
    keepPage: false, harvest: false, port: null, url: SITE, retries: 1, retryDelay: 20,
    root: null, dirs: null, all: false, skipExisting: false, plan: false, uploadTimeoutMs: 180000,
  };
  const takesValue = new Set(['--profile', '--profiles', '--prompt', '--prompt-file', '--out',
    '--timeout', '--login-timeout', '--fit-mode', '--fit', '--url', '--port', '--retries', '--retry-delay',
    '--root', '--dirs', '--dir', '--upload-timeout']);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (takesValue.has(a)) {
      const v = argv[++i];
      if (v === undefined) die(`${a} 缺少参数值`);
      if (a === '--profile' || a === '--profiles') o.profileSel = (o.profileSel ?? []).concat(v.split(','));
      else if (a === '--root') o.root = v;
      else if (a === '--dirs' || a === '--dir') o.dirs = (o.dirs ?? []).concat(v.split(','));
      else if (a === '--prompt') o.prompt = v;
      else if (a === '--prompt-file') o.prompt = fs.readFileSync(expand(v), 'utf8').trim();
      else if (a === '--out') o.out = expand(v);
      else if (a === '--timeout') o.timeoutMin = Number(v);
      else if (a === '--login-timeout') o.loginTimeoutMin = Number(v);
      else if (a === '--fit-mode') o.fitMode = v;
      else if (a === '--port') o.port = Number(v);
      else if (a === '--retries') o.retries = Number(v);
      else if (a === '--upload-timeout') o.uploadTimeoutMs = Number(v) * 1000;
      else if (a === '--retry-delay') o.retryDelay = Number(v);
      else if (a === '--fit') o.fitRatio = v.includes(':') ? (() => { const [w, h] = v.split(':').map(Number); return w / h; })() : Number(v);
      else if (a === '--url') o.url = v;
      continue;
    }
    if (a === '--no-fit') o.fit = false;
    else if (a === '--debug') o.debug = true;
    else if (a === '--keep') o.keep = true;
    else if (a === '--dry') o.dry = true;
    else if (a === '--probe') o.probe = true;
    else if (a === '--login') o.login = true;
    else if (a === '--force') o.force = true;
    else if (a === '--list') o.list = true;
    else if (a === '--as-cover') o.asCover = true;
    else if (a === '--all') o.all = true;
    else if (a === '--skip-existing') o.skipExisting = true;
    else if (a === '--plan') o.plan = true;
    else if (a === '--cleanup') o.cleanup = true;
    else if (a === '--close-only') o.closeOnly = true;
    else if (a === '--keep-page') o.keepPage = true;
    else if (a === '--harvest') { o.harvest = true; o.keepPage = true; }
    else if (a === '-h' || a === '--help') { printHelp(); process.exit(0); }
    else if (a.startsWith('--')) die(`未知选项 ${a}（--help 查看用法）`);
    else if (!o.input) o.input = a;
    else die(`多余的参数：${a}`);
  }
  return o;
}

function printHelp() {
  const src = fs.readFileSync(fileURLToPath(import.meta.url), 'utf8');
  console.log(src.slice(src.indexOf('/**'), src.indexOf('*/') + 2));
}

// ───────────────────────── profile / 状态 ─────────────────────────
function loadProfiles() {
  if (!fs.existsSync(PROFILES_FILE)) {
    die(`未找到 ${PROFILES_FILE}\n请先复制 ${PROFILES_EXAMPLE_FILE} 为 cg_profiles.json 并配置本机账号。`);
  }
  const cfg = JSON.parse(fs.readFileSync(PROFILES_FILE, 'utf8'));
  return (cfg.profiles ?? []).map((p, i) => ({ ...p, dir: expand(p.dir), _idx: i, port: p.port ?? DEFAULT_PORT + i }));
}

function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return { exhausted: {} }; } }
function saveState(s) { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2), 'utf8'); }

function exhaustedUntil(st, name) {
  const t = st.exhausted?.[name];
  if (!t) return null;
  const ts = Date.parse(t);
  if (!Number.isFinite(ts)) return null;
  const resetAt = ts + 24 * 3600 * 1000;
  return Date.now() < resetAt ? new Date(resetAt) : null;
}

function selectProfiles(all, sel, st, force) {
  let list;
  if (sel?.length) {
    list = sel.map((s) => {
      const byName = all.find((p) => p.name === s);
      if (byName) return byName;
      if (/^\d+$/.test(s) && all[Number(s) - 1]) return all[Number(s) - 1];
      die(`找不到 profile: ${s}\n可选：${all.map((p, i) => `${p.name}(${i + 1})`).join(', ')}`);
    });
  } else list = all.filter((p) => p.enabled !== false);
  if (!list.length) die('没有可用的 profile，检查 cg_profiles.json');

  const usable = [], skipped = [];
  for (const p of list) {
    const until = (sel?.length || force) ? null : exhaustedUntil(st, p.name);
    if (until) skipped.push(`${p.name}（额度耗尽，${until.toLocaleString('zh-CN')} 后恢复）`);
    else usable.push(p);
  }
  if (skipped.length) log('状态', '跳过：' + skipped.join('；'));
  if (!usable.length) die('所有账号都在额度耗尽状态，加 --force 强制重试');
  return usable;
}

// ───────────────────────── 浏览器（CDP 复用）─────────────────────────
function findEdge() {
  const cands = [
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env['ProgramFiles'] && path.join(process.env['ProgramFiles'], 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
  ].filter(Boolean);
  for (const c of cands) if (fs.existsSync(c)) return c;
  die('找不到 msedge.exe');
}

const cdpUrl = (port) => `http://127.0.0.1:${port}`;

async function cdpAlive(port) {
  try {
    const r = await fetch(`${cdpUrl(port)}/json/version`, { signal: AbortSignal.timeout(1500) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

// ── 端口归属：只认自己的浏览器 ──
// 「端口活着」不等于「是本 profile 的浏览器」：别的工具（bili_upload 的自动化 Edge）、
// 别的 Chrome、别的 profile 都可能正好占着这个端口。以前直接复用，结果把 ChatGPT 开进了
// 别人的 session（看着像「没登录」，其实是根本没用自己的 profile）。现在按端口占用者认：
// 必须是 msedge.exe 浏览器主进程，且 --user-data-dir / --profile-directory 和本 profile 一致。
const normPath = (p) => path.resolve(p).replace(/[\\/]+$/, '').toLowerCase();
const normPd = (pd) => (pd || 'Default').toLowerCase();
const expandWin = (p) => p.replace(/%([^%]+)%/g, (m, k) => process.env[k] ?? m);
const grabArg = (cmdline, re) => { const m = cmdline.match(re); return m ? (m[1] ?? m[2]) : null; };

/**
 * 谁在监听 :port？返回 { state: 'none'|'mine'|'foreign'|'unknown', who }
 * 查不到（非 Windows、没权限、PowerShell 被挡）按 unknown 处理，不拦。
 */
async function portOwner(port, prof) {
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
  if (!/^msedge\.exe$/i.test(name)) return { state: 'foreign', who: `${name}（不是 Edge）` };
  if (/--type=/.test(cmdline)) return { state: 'foreign', who: 'Edge 的子进程（不是浏览器主进程）' };
  const dir = grabArg(cmdline, /--user-data-dir=(?:"([^"]+)"|([^\s"]+))/);
  const pd = grabArg(cmdline, /--profile-directory=(?:"([^"]+)"|([^\s"]+))/);
  const who = `Edge（--user-data-dir=${dir ?? '默认 profile' }${pd ? ` --profile-directory=${pd}` : ''}）`;
  if (!dir) return { state: 'unknown', who };
  const same = normPath(expandWin(dir)) === normPath(prof.dir) && normPd(pd) === normPd(prof.profileDirectory);
  return { state: same ? 'mine' : 'foreign', who };
}

function diePortForeign(prof, who) {
  die([
    `:${prof.port} 上的浏览器不是 ${prof.name} 的 —— 别的 session 占着这个端口：`,
    `  占用者: ${who}`,
    `  本 profile 应为: --user-data-dir=${prof.dir}${prof.profileDirectory ? ` --profile-directory=${prof.profileDirectory}` : ''}`,
    `  不能往上复用（复用了就是「打开的是别人的浏览器，没登录态」）。`,
    `  换个端口（--port <n>，或改 cg_profiles.json 里 ${prof.name} 的 port），或先关掉占用它的浏览器。`,
  ].join('\n'));
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

/** 端口没人听 → 独立启动一个浏览器（detached，脚本退出后它继续活着） */
async function startBrowser(prof, opts) {
  const bindErr = await portBindError(prof.port);
  if (bindErr) die(`端口 :${prof.port} 起不了浏览器：${bindErr}。\n  换个端口再跑：--port <n>（或改 cg_profiles.json 里 ${prof.name} 的 port）`);
  const exe = findEdge();
  const args = [
    `--remote-debugging-port=${prof.port}`,
    `--user-data-dir=${prof.dir}`,
    '--no-first-run', '--no-default-browser-check', '--hide-crash-restore-bubble',
    '--disable-blink-features=AutomationControlled',
    '--disable-session-crashed-bubble',
  ];
  if (prof.profileDirectory) args.push(`--profile-directory=${prof.profileDirectory}`);
  args.push(opts.keepPage ? 'about:blank' : opts.url);
  const fresh = !fs.existsSync(path.join(prof.dir, 'Default'));
  log('启动', `${exe} --remote-debugging-port=${prof.port} --user-data-dir="${prof.dir}"${prof.profileDirectory ? ` --profile-directory="${prof.profileDirectory}"` : ''}`);
  const child = spawn(exe, args, { detached: true, stdio: 'ignore', windowsHide: false });
  child.unref();
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const v = await cdpAlive(prof.port);
    if (v) {
      // 端口活了不代表就是我们刚起的那个：万一被别的浏览器抢先占住，起来的进程绑不上端口，
      // 直接复用就又串到别人的 session 上了
      const own = await portOwner(prof.port, prof);
      if (own.state === 'foreign') diePortForeign(prof, own.who);
      log('启动', `CDP 就绪 (:${prof.port}, ${v.Browser})`);
      return { port: prof.port, spawned: true, fresh };
    }
    await sleep(1000);
  }
  die([
    `浏览器起来了但 CDP 端口 :${prof.port} 连不上。常见原因：`,
    `  · 该 user-data-dir 已被别的浏览器实例占用（日常 Edge 开着？先完全退出 Edge）`,
    `  · 启动时弹了对话框挡住了`,
  ].join('\n'));
}

/** 复用或新建浏览器 + ChatGPT 标签页 */
async function ensureSession(prof, opts) {
  const alive = await cdpAlive(prof.port);
  let spawned = false, fresh = false;
  if (alive) {
    const own = await portOwner(prof.port, prof);
    if (own.state === 'foreign') diePortForeign(prof, own.who);
    if (own.state === 'unknown') log('复用', `(!) 认不出 :${prof.port} 的占用者，无法确认是本 profile 的浏览器，先按复用处理`);
    log('复用', `已发现浏览器 :${prof.port}（${alive.Browser}），直接复用`);
  } else {
    const r = await startBrowser(prof, opts);
    spawned = r.spawned; fresh = r.fresh;
  }
  const browser = await chromium.connectOverCDP(cdpUrl(prof.port), { timeout: 30000 });
  const ctx = browser.contexts()[0] ?? await browser.newContext();
  let page = ctx.pages().find((p) => /chatgpt\.com|openai\.com/.test(p.url()));
  if (!page) {
    const blank = ctx.pages().find((p) => /^(about:blank|edge:\/\/)/.test(p.url()));
    page = blank ?? await ctx.newPage();
    log('标签页', '没有 ChatGPT 标签页，新建一个');
  } else {
    log('标签页', `复用已有标签页：${page.url().slice(0, 70)}`);
  }
  // 首次创建的 profile 常带一堆 Edge 引导页，顺手清掉
  if (spawned && fresh) {
    for (const p of ctx.pages()) {
      if (p === page) continue;
      if (/^(edge:\/\/|about:|https:\/\/www\.microsoft\.com|https:\/\/go\.microsoft\.com)/.test(p.url())) {
        await p.close().catch(() => {});
      }
    }
  }
  await page.bringToFront().catch(() => {});
  page.setDefaultTimeout(30000);
  return { browser, ctx, page, port: prof.port };
}

/**
 * 收尾：
 *   默认什么都不做（浏览器与标签页都留着，下次直接复用）
 *   --cleanup 关闭 ChatGPT 标签页；--close-only 或 --cleanup 时若还有 --cleanup 则连浏览器一起关
 */
async function teardown(session, opts) {
  if (!session) return;
  if (!opts.cleanup) {
    log('收尾', `保留浏览器与标签页（下次直接复用）；要清理就加 --cleanup`);
    return;
  }
  log('收尾', '--cleanup：关闭 ChatGPT 标签页并结束浏览器');
  await session.page.close().catch(() => {});
  try { await session.browser.close(); } catch { /* CDP 断开即可 */ }
  await killByPort(session.port);
}

async function killByPort(port) {
  const { execFile } = await import('node:child_process');
  const { promisify } = await import('node:util');
  const ps = `$p=(Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess); if($p){Stop-Process -Id $p -Force; "killed $p"} else {"no listener"}`;
  try {
    const { stdout } = await promisify(execFile)('powershell', ['-NoProfile', '-Command', ps], { timeout: 20000 });
    log('收尾', `端口 :${port} → ${stdout.trim()}`);
  } catch (e) { log('收尾', `结束浏览器失败: ${e.message.split('\n')[0]}`); }
}

// ───────────────────────── 页面辅助 ─────────────────────────
async function firstVisible(page, selectors, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    for (const s of selectors) {
      try {
        const loc = page.locator(s).first();
        if (await loc.count() && await loc.isVisible()) return loc;
      } catch { /* 导航中会抛错 */ }
    }
    await sleep(300);
  }
  return null;
}

async function dumpDebug(page, label, opts) {
  if (!opts.debug) return;
  ensureDir(DEBUG_DIR);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  try {
    await page.screenshot({ path: path.join(DEBUG_DIR, `${label}_${stamp}.png`) });
    const dom = await page.evaluate(() => ({
      href: location.href,
      buttons: [...document.querySelectorAll('button,[role=button],a[download]')].filter((b) => b.offsetParent !== null)
        .map((b) => ({ label: b.getAttribute('aria-label'), testid: b.getAttribute('data-testid'), text: (b.innerText || '').trim().slice(0, 30) })),
      imgs: [...document.querySelectorAll('img')].filter((i) => i.naturalWidth >= 128)
        .map((i) => ({ src: (i.currentSrc || i.src).slice(0, 140), w: i.naturalWidth, h: i.naturalHeight, alt: (i.alt || '').slice(0, 60) })),
      bodyTail: (document.body.innerText || '').slice(-2000),
    }));
    fs.writeFileSync(path.join(DEBUG_DIR, `${label}_${stamp}.json`), JSON.stringify(dom, null, 2), 'utf8');
    log('调试', `${label} → cg_debug/${label}_${stamp}.{png,json}`);
  } catch (e) { log('调试', `快照失败: ${e.message.split('\n')[0]}`); }
}

async function isLoggedIn(page) {
  try {
    const hasLogin = await Promise.all(SEL.loginButton.map((s) => page.locator(s).count()))
      .then((counts) => counts.some((n) => n > 0));
    const hasEditor = await Promise.all(SEL.editor.map((s) => page.locator(s).count()))
      .then((counts) => counts.some((n) => n > 0));
    return !hasLogin && hasEditor;
  } catch { return false; }
}

async function ensureLoggedIn(page, opts, label) {
  if (await isLoggedIn(page)) { log(label, '已登录 ✓'); return true; }
  if (opts.loginTimeoutMin <= 0) {
    log(label, '未登录（--login-timeout 0，不等待）');
    return false;
  }
  const mins = opts.loginTimeoutMin;
  console.log(`
┌────────────────────────────────────────────────────────────────┐
│  需要手动登录 ChatGPT                                          │
│                                                                │
│  请在浏览器窗口里登录（可能要过 Cloudflare 人机验证）。         │
│  登录成功后脚本自动继续，最长等 ${String(mins).padEnd(2)} 分钟。                   │
│  登录态长期保存在该 profile 里，浏览器也会留着，以后无需再登。  │
└────────────────────────────────────────────────────────────────┘`);
  const deadline = Date.now() + mins * 60 * 1000;
  let lastTick = 0, ok = false;
  while (Date.now() < deadline) {
    if (page.isClosed()) { log(label, '窗口被关闭，停止等待登录'); return false; }
    if (await isLoggedIn(page)) { ok = true; break; }
    if (Date.now() - lastTick > 15000) {
      lastTick = Date.now();
      log(label, `等待登录中… 剩余 ${Math.ceil((deadline - Date.now()) / 60000)} 分钟`);
    }
    await sleep(2000);
  }
  if (!ok) { log(label, '等待登录超时'); return false; }
  log(label, '检测到登录成功 ✓');
  await sleep(3000);
  return true;
}

async function goHome(page, opts) {
  if (opts.keepPage) { log('标签页', '--keep-page：保持当前页面不重载'); return; }
  await page.goto(opts.url, { waitUntil: 'domcontentloaded', timeout: 90000 }).catch((e) => log('导航', e.message.split('\n')[0]));
  await sleep(3000);
}

// ───────────────────────── 上传 / 输入 / 发送 ─────────────────────────
async function hasAttachment(page) {
  return await page.evaluate(() => {
    const scope = document.querySelector('form') || document.body;
    const imgs = [...scope.querySelectorAll('img')].filter((i) => {
      const s = i.currentSrc || i.src || '';
      return s.startsWith('blob:') || s.includes('oaiusercontent') || s.includes('backend-api');
    });
    if (imgs.length) return true;
    return !!scope.querySelector('button[aria-label*="移除"], button[aria-label*="删除附件"], button[aria-label*="Remove file"]');
  }).catch(() => false);
}

/**
 * 等缩略图上层的上传进度环消失。
 * 上传中：form 里存在带 stroke-dasharray 的 circle（进度环），同时缩略图外层带 .cursor-wait；
 * 两者同时消失即为上传完成。要求连续两次轮询都干净，避免中间态抖动。
 */
async function waitUploadDone(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let clean = 0;
  while (Date.now() < deadline) {
    const busy = await page.evaluate((sels) => {
      const has = (s) => { try { return !!document.querySelector(s); } catch { return false; } };
      return sels.uploadBusy.some(has);
    }, SEL).catch(() => true);
    if (!busy) { clean++; if (clean >= 2) return true; }
    else clean = 0;
    await sleep(400);
  }
  return false;
}

async function attachImage(page, file, label, opts) {
  const inputs = page.locator('input[type="file"]');
  const n = await inputs.count();
  if (!n) { await dumpDebug(page, 'no_file_input', opts); die('页面上找不到 input[type=file]，ChatGPT 结构可能变了'); }
  const order = [];
  for (let i = 0; i < n; i++) {
    const acc = (await inputs.nth(i).getAttribute('accept')) ?? '';
    if (acc.includes('image')) order.push(i);
  }
  log(label, `候选上传入口：input#${order.join(', #')}`);
  for (const i of order) {
    log(label, `尝试上传（input#${i}）…`);
    try { await inputs.nth(i).setInputFiles(file, { timeout: 30000 }); }
    catch (e) { log(label, `setInputFiles 失败: ${e.message.split('\n')[0]}`); continue; }
    const deadline = Date.now() + 45000;
    let appeared = false;
    while (Date.now() < deadline) {
      if (await hasAttachment(page)) { appeared = true; break; }
      await sleep(500);
    }
    if (appeared) {
      log(label, '缩略图已出现，等待上传完成…');
      const t0 = Date.now();
      if (!await waitUploadDone(page, opts.uploadTimeoutMs)) {
        log(label, `上传超时：${opts.uploadTimeoutMs / 1000}s 内进度环一直没消失`);
        await dumpDebug(page, 'upload_timeout', opts);
        return 'upload-timeout';
      }
      log(label, `上传完成 ✓（耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s）`);
      return true;
    }
    log(label, '附件没出现，换下一个入口');
    await page.keyboard.press('Escape').catch(() => {});
    await sleep(1000);
  }
  await dumpDebug(page, 'attach_failed', opts);
  return false;
}

async function typePrompt(page, text, label, opts) {
  const ed = await firstVisible(page, SEL.editor, 20000);
  if (!ed) { await dumpDebug(page, 'no_editor', opts); die('找不到输入框'); }
  await ed.click();
  await ed.fill('').catch(() => {});
  const lines = String(text).split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (i) await page.keyboard.press('Shift+Enter');
    if (lines[i]) await page.keyboard.insertText(lines[i]);
  }
  await sleep(500);
  const got = await ed.evaluate((el) => (el.innerText || el.value || '').trim());
  if (!got) { await dumpDebug(page, 'prompt_empty', opts); die('提示词没写进输入框'); }
  log(label, `提示词已填入：${got.slice(0, 36)}${got.length > 36 ? '…' : ''}`);
}

async function sendMessage(page, label) {
  const btn = await firstVisible(page, SEL.sendButton, 5000);
  if (btn) await btn.click();
  else { log(label, '没找到发送按钮，改用 Enter 发送'); await page.keyboard.press('Enter'); }
  await sleep(2000);
  const leftover = await page.evaluate((sels) => {
    const el = document.querySelector(sels[0]) || document.querySelector(sels[1]);
    return el ? (el.innerText || el.value || '').trim().length : 0;
  }, SEL.editor).catch(() => 0);
  if (leftover > 5) { log(label, '输入框仍有内容，补按 Enter'); await page.keyboard.press('Enter'); }
}

// ───────────────────────── 等待生成结果 ─────────────────────────
async function readState(page, excludeNames = []) {
  return await page.evaluate(({ sels, excludeNames }) => {
    const txt = (el) => (el?.innerText || '').trim();
    const has = (list) => list.some((s) => { try { return !!document.querySelector(s); } catch { return false; } });
    const turns = [...document.querySelectorAll(sels.assistantTurn[0])];
    const last = turns[turns.length - 1];
    // 用户上传的原图绝不能算作生成结果；同一张生成图在 DOM 里会出现两次（主图 + 背景层），按 src 去重
    const userSrcs = new Set([...document.querySelectorAll('[data-message-author-role="user"] img')]
      .map((i) => i.currentSrc || i.src || ''));
    const pick = (list) => {
      const seen = new Set(), out = [];
      for (const i of list) {
        const s = i.currentSrc || i.src || '';
        if (!s || seen.has(s) || userSrcs.has(s)) continue;
        if (excludeNames.includes(i.alt)) continue;
        if (i.naturalWidth < 256 || i.naturalHeight < 256 || /avatar|icon/i.test(i.alt)) continue;
        seen.add(s);
        out.push({ src: s, w: i.naturalWidth, h: i.naturalHeight, alt: i.alt || '' });
      }
      return out;
    };
    let imgs = pick([...document.querySelectorAll(sels.genImg.join(','))]);
    // 兜底：万一出图容器类名变了，退回「assistant 回合里的大图」
    if (!imgs.length && last) imgs = pick([...last.querySelectorAll('img')]);
    return {
      stop: has(sels.stopButton),
      placeholder: has(sels.genPlaceholder),
      imgs, turnCount: turns.length,
      lastText: txt(last).slice(-2500),
      dialogText: [...document.querySelectorAll(sels.dialog.join(','))].map(txt).join('\n').slice(-1500),
      bodyTail: (document.body.innerText || '').slice(-2500),
    };
  }, { sels: SEL, excludeNames });
}

const matchAny = (text, pats) => !!text && pats.some((r) => r.test(text));

/** 等一次生成结束 → { type: 'image'|'quota'|'unavailable'|'refusal'|'noimage'|'timeout' } */
async function waitForOutcome(page, label, opts) {
  const startedAt = Date.now();
  const deadline = startedAt + opts.timeoutMin * 60 * 1000;
  const excludeNames = opts.image ? [path.basename(opts.image)] : [];
  let stable = 0, prevKey = '', noImgSince = 0, tick = 0;
  while (Date.now() < deadline) {
    await sleep(1500);
    if (page.isClosed()) return { type: 'timeout', reason: '页面被关闭' };
    let st;
    try { st = await readState(page, excludeNames); } catch { continue; }
    tick++;

    const all = [st.dialogText, st.lastText, st.bodyTail].join('\n');
    if (matchAny(all, PAT.quota)) {
      const src = matchAny(st.dialogText, PAT.quota) ? st.dialogText : matchAny(st.lastText, PAT.quota) ? st.lastText : st.bodyTail;
      return { type: 'quota', text: src.slice(-400) };
    }
    if (matchAny([st.dialogText, st.lastText].join('\n'), PAT.unavailable)) {
      return { type: 'unavailable', text: (st.dialogText || st.lastText).slice(-400) };
    }

    const key = st.imgs.map((i) => i.src).join('|');
    if (st.imgs.length && !st.stop) {
      stable = key === prevKey ? stable + 1 : 0;
      if (stable >= 2) return { type: 'image', imgs: st.imgs };
    } else stable = 0;
    prevKey = key;

    if (!st.stop && matchAny(st.lastText, PAT.refusal)) return { type: 'refusal', text: st.lastText.slice(-400) };
    if (!st.stop && matchAny(st.lastText, PAT.genError)) return { type: 'generror', text: st.lastText.slice(-400) };

    if (!st.stop && !st.imgs.length && !st.placeholder && st.lastText.length > 20) {
      if (!noImgSince) noImgSince = Date.now();
      else if (Date.now() - noImgSince > 45000) return { type: 'noimage', text: st.lastText.slice(-600) };
    } else noImgSince = 0;

    if (tick % 10 === 0) {
      log(label, `等待中 ${Math.round((Date.now() - startedAt) / 1000)}s${st.stop ? '（流式输出中）' : ''}${st.placeholder ? '（图片生成中）' : ''}`);
    }
  }
  return { type: 'timeout', reason: `${opts.timeoutMin} 分钟未完成` };
}

// ───────────────────────── 下载与后处理 ─────────────────────────
async function fetchBytes(page, src) {
  if (src.startsWith('data:')) return Buffer.from(src.split(',')[1], 'base64');
  if (src.startsWith('blob:')) {
    const dataUrl = await page.evaluate(async (s) => {
      const r = await fetch(s);
      const b = await r.blob();
      return await new Promise((res) => { const f = new FileReader(); f.onload = () => res(f.result); f.readAsDataURL(b); });
    }, src);
    return Buffer.from(dataUrl.split(',')[1], 'base64');
  }
  const resp = await page.request.get(src, { timeout: 180000 });
  if (!resp.ok()) throw new Error(`HTTP ${resp.status()}`);
  return Buffer.from(await resp.body());
}

function imageSize(buf) {
  if (buf.length > 24 && buf[0] === 0x89 && buf[1] === 0x50) return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
  if (buf.length > 4 && buf[0] === 0xff && buf[1] === 0xd8) {
    let o = 2;
    while (o < buf.length - 9) {
      if (buf[o] !== 0xff) { o++; continue; }
      const m = buf[o + 1];
      if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) return { h: buf.readUInt16BE(o + 5), w: buf.readUInt16BE(o + 7) };
      o += 2 + buf.readUInt16BE(o + 2);
    }
  }
  return { w: 0, h: 0 };
}

/** 依次尝试：抓 src 字节 → 点下载按钮 → 元素截图 */
async function saveImage(page, img, outPath, label) {
  const write = (buf, via) => {
    ensureDir(path.dirname(outPath));
    fs.writeFileSync(outPath, buf);
    const { w, h } = imageSize(buf);
    log(label, `已保存 ${w}x${h} ${(buf.length / 1048576).toFixed(1)}MB (via ${via}) → ${outPath}`);
    return { ok: true, w, h, via, path: outPath };
  };
  try {
    const buf = await fetchBytes(page, img.src);
    const { w } = imageSize(buf);
    if (buf.length > 20000 && w >= 512) return write(buf, 'src');
    log(label, `抓到的图偏小(${w}px/${buf.length}B)，改用下载按钮`);
  } catch (e) { log(label, `抓 src 失败: ${e.message.split('\n')[0]}`); }

  try {
    const imgEl = page.locator(`img[src="${img.src}"]`).first();
    if (await imgEl.count()) {
      await imgEl.hover({ timeout: 5000 }).catch(() => {});
      await sleep(800);
      const btn = await firstVisible(page, SEL.downloadButton, 4000);
      if (btn) {
        const [dl] = await Promise.all([
          page.waitForEvent('download', { timeout: 60000 }).catch(() => null),
          btn.click({ timeout: 5000 }).catch(() => {}),
        ]);
        if (dl) { await dl.saveAs(outPath); return write(fs.readFileSync(outPath), 'download'); }
      }
    }
  } catch (e) { log(label, `下载按钮路径失败: ${e.message.split('\n')[0]}`); }

  try {
    const buf = await page.locator(`img[src="${img.src}"]`).first().screenshot({ timeout: 15000 });
    return write(buf, 'screenshot');
  } catch (e) { log(label, `元素截图失败: ${e.message.split('\n')[0]}`); }

  return { ok: false };
}

/** 用浏览器 canvas 做比例归一化，免依赖图像库 */
async function fitToRatio(page, fileIn, fileOut, ratio, mode) {
  const dataUrl = 'data:image/png;base64,' + fs.readFileSync(fileIn).toString('base64');
  const out = await page.evaluate(async ({ dataUrl, ratio, mode }) => {
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = dataUrl; });
    const sw = img.naturalWidth, sh = img.naturalHeight;
    let tw, th;
    if (sw / sh >= ratio) { th = sh; tw = Math.round(sh * ratio); }
    else { tw = sw; th = Math.round(sw / ratio); }
    const cv = document.createElement('canvas');
    cv.width = tw; cv.height = th;
    const ctx = cv.getContext('2d');
    if (mode === 'pad') {
      const sc = Math.max(tw / sw, th / sh);
      ctx.filter = 'blur(60px)';
      ctx.drawImage(img, (tw - sw * sc) / 2, (th - sh * sc) / 2, sw * sc, sh * sc);
      ctx.filter = 'none';
      const sc2 = Math.min(tw / sw, th / sh);
      ctx.drawImage(img, (tw - sw * sc2) / 2, (th - sh * sc2) / 2, sw * sc2, sh * sc2);
    } else {
      ctx.drawImage(img, Math.max(0, (sw - tw) / 2), Math.max(0, (sh - th) / 2), tw, th, 0, 0, tw, th);
    }
    return { url: cv.toDataURL('image/png'), w: tw, h: th };
  }, { dataUrl, ratio, mode });
  fs.writeFileSync(fileOut, Buffer.from(out.url.split(',')[1], 'base64'));
  return out;
}

// ───────────────────────── 输入解析 ─────────────────────────
function resolveInput(input) {
  const cands = [];
  if (path.isAbsolute(input) || input.includes('/') || input.includes('\\')) cands.push(expand(input));
  else {
    cands.push(path.join(WORKSPACE_DIR, input, '封面.jpg'));
    cands.push(path.join(WORKSPACE_DIR, input, '封面.png'));
    cands.push(path.join(WORKSPACE_DIR, input));
    cands.push(expand(input));
  }
  for (const c of cands) {
    if (!fs.existsSync(c)) continue;
    if (fs.statSync(c).isFile()) return c;
    for (const name of ['封面.jpg', '封面.png']) {
      const inner = path.join(c, name);
      if (fs.existsSync(inner)) return inner;
    }
  }
  die(`找不到封面图，试过：\n  ${cands.join('\n  ')}`);
}

// ───────────────────────── 单账号一轮 ─────────────────────────
// 这些失败多半是偶发的，值得在同一账号上重试；额度/未登录/套餐问题重试也没用
const RETRYABLE = new Set(['generror', 'noimage', 'refusal', 'timeout', 'download-failed', 'upload-failed', 'upload-timeout', 'error']);

async function runProfile(prof, opts) {
  const attempts = Math.max(1, (opts.retries ?? 0) + 1);
  let r = null;
  for (let i = 1; i <= attempts; i++) {
    if (i > 1) log(prof.name, `重试第 ${i}/${attempts} 次（回到新会话重新上传）`);
    r = await attemptOnce(prof, opts);
    if (!RETRYABLE.has(r.status)) return r;
    if (i < attempts) {
      log(prof.name, `本轮失败（${r.status}）${r.text ? '：' + r.text.replace(/\s+/g, ' ').slice(0, 120) : ''}，${opts.retryDelay}s 后重试`);
      await sleep(opts.retryDelay * 1000);
    }
  }
  return r;
}

async function attemptOnce(prof, opts) {
  const label = prof.name;
  const session = await ensureSession(prof, opts);
  const { page } = session;
  try {
    await goHome(page, opts);

    if (opts.login) return { status: 'hold', session, reason: 'login' };
    if (opts.probe) return { status: 'hold', session, reason: 'probe' };
    if (!await ensureLoggedIn(page, opts, label)) return { status: 'not-logged-in' };

    if (!opts.harvest) {
      const up = await attachImage(page, opts.image, label, opts);
      if (up !== true) return { status: up === 'upload-timeout' ? 'upload-timeout' : 'upload-failed' };
      await typePrompt(page, opts.prompt, label, opts);
      await dumpDebug(page, `${label}_ready`, opts);

      if (opts.dry) {
        console.log(`\n>>> --dry：已停在发送前，去浏览器窗口检查即可（浏览器和标签页都会留着）\n`);
        return { status: 'done-dry' };
      }

      await sendMessage(page, label);
      log(label, '已发送，等待生成…');
    } else {
      log(label, '--harvest：不重发，直接抓当前页面上的图');
    }

    const outcome = await waitForOutcome(page, label, opts);
    await dumpDebug(page, `${label}_${outcome.type}`, opts);
    if (outcome.type !== 'image') {
      if (outcome.text) log(label, `回复内容：${outcome.text.replace(/\s+/g, ' ').slice(0, 200)}`);
      return { status: outcome.type, text: outcome.text };
    }

    log(label, `检测到 ${outcome.imgs.length} 张图，开始下载`);
    const saved = [];
    for (let i = 0; i < outcome.imgs.length; i++) {
      const finalPath = i === 0 ? opts.out : opts.out.replace(/(\.png)$/, `_${i + 1}$1`);
      const rawPath = opts.fit ? finalPath.replace(/(\.png)$/, '_raw$1') : finalPath;
      const r = await saveImage(page, outcome.imgs[i], rawPath, label);
      if (r.ok) saved.push({ ...r, finalPath, rawPath });
    }
    if (!saved.length) return { status: 'download-failed', session };

    if (opts.fit && saved[0]) {
      const dim = await fitToRatio(page, saved[0].rawPath, saved[0].finalPath, opts.fitRatio, opts.fitMode);
      log(label, `比例归一化 ${opts.fitMode} → ${dim.w}x${dim.h} → ${saved[0].finalPath}`);
    }
    return { status: 'ok', saved, session };
  } catch (e) {
    log(label, `异常: ${e.message.split('\n')[0]}`);
    return { status: 'error', text: e.message, session };
  }
}

// ───────────────────────── 主流程 ─────────────────────────
const opts = parseArgs(process.argv.slice(2));
const all = loadProfiles();
const st = loadState();

if (opts.list) {
  console.log('profile 列表：');
  for (const [i, p] of all.entries()) {
    const until = exhaustedUntil(st, p.name);
    const aliveJson = await cdpAlive(p.port);
    let alive = '浏览器未启动';
    if (aliveJson) {
      const own = await portOwner(p.port, p);
      alive = own.state === 'mine' ? '浏览器在线（本 profile）'
        : own.state === 'foreign' ? `端口被占用：${own.who}  ← 不是本 profile，不会被复用`
          : `浏览器在线，但认不出占用者（${aliveJson.Browser}）`;
    }
    console.log(`  ${i + 1}. ${p.name}${p.enabled === false ? ' [默认不启用]' : ''}  :${p.port}  ${alive}`);
    console.log(`     ${p.dir}${p.profileDirectory ? `  (--profile-directory=${p.profileDirectory})` : ''}`);
    console.log(`     状态: ${until ? `额度耗尽，预计 ${until.toLocaleString('zh-CN')} 恢复` : '可用'}${p.note ? `  · ${p.note}` : ''}`);
  }
  console.log('\n提示：--cleanup 会在结束时关掉标签页与浏览器；默认不清理，便于反复调试。');
  process.exit(0);
}

if (opts.closeOnly) {
  for (const p of all) {
    if (await cdpAlive(p.port)) {
      log('关闭', `${p.name} :${p.port}`);
      const browser = await chromium.connectOverCDP(cdpUrl(p.port)).catch(() => null);
      if (browser) {
        for (const ctx of browser.contexts()) for (const pg of ctx.pages()) await pg.close().catch(() => {});
        await browser.close().catch(() => {});
      }
      await killByPort(p.port);
    }
  }
  process.exit(0);
}

const profiles = selectProfiles(all, opts.profileSel, st, opts.force);
if (opts.port) {
  if (profiles.length > 1) die('--port 只能在单个 profile 时使用');
  profiles[0].port = opts.port;
}

if (opts.login) {
  log('模式', `依次打开 ${profiles.length} 个 profile 手动登录（关掉标签页即跳过）`);
  for (const p of profiles) {
    let session = null;
    try {
      session = await ensureSession(p, opts);
      await goHome(session.page, opts);
      const ok = await ensureLoggedIn(session.page, opts, p.name);
      log(p.name, ok ? '✓ 登录态已保存' : '未完成登录');
    } finally {
      await teardown(session, opts);
    }
  }
  process.exit(0);
}

if (opts.probe) {
  const p = profiles[0];
  const session = await ensureSession(p, opts);
  await goHome(session.page, opts);
  await ensureLoggedIn(session.page, opts, p.name);
  ensureDir(DEBUG_DIR);
  log('probe', `每 15s 抓一次 DOM → cg_debug/probe_latest.{png,json}；去页面上手动生成一张图，完事 Ctrl+C`);
  for (let i = 0; ; i++) {
    const dom = await session.page.evaluate(() => ({
      href: location.href,
      buttons: [...document.querySelectorAll('button,[role=button],a[download]')].filter((b) => b.offsetParent !== null)
        .map((b) => ({ label: b.getAttribute('aria-label'), testid: b.getAttribute('data-testid'), text: (b.innerText || '').trim().slice(0, 30) })),
      imgs: [...document.querySelectorAll('img')].filter((i) => i.naturalWidth >= 128)
        .map((i) => ({ src: (i.currentSrc || i.src).slice(0, 200), w: i.naturalWidth, h: i.naturalHeight, alt: (i.alt || '').slice(0, 80), cls: (i.className || '').toString().slice(0, 100) })),
      turns: [...document.querySelectorAll('[data-message-author-role]')].map((t) => ({
        role: t.getAttribute('data-message-author-role'), testid: t.getAttribute('data-testid'),
        text: (t.innerText || '').slice(0, 300),
      })),
      bodyTail: (document.body.innerText || '').slice(-3000),
    })).catch(() => null);
    if (dom) {
      fs.writeFileSync(path.join(DEBUG_DIR, 'probe_latest.json'), JSON.stringify(dom, null, 2), 'utf8');
      await session.page.screenshot({ path: path.join(DEBUG_DIR, 'probe_latest.png') }).catch(() => {});
      log('probe', `#${i} 已记录（${dom.imgs.length} 张大图 / ${dom.turns.length} 条消息）`);
    }
    await sleep(15000);
  }
}

// ───────────────────────── 批量目标解析 ─────────────────────────
let RESOLVE_SKIPPED = [];
// 支持形如 0901-0905 的连续编号简写（保留前导零）
function expandDirSpec(spec) {
  const m = String(spec).match(/^(\d+)-(\d+)$/);
  if (!m) return [spec];
  const [, a, b] = m;
  const from = Number(a), to = Number(b);
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return [spec];
  if (to - from > 200) die(`目录范围过大：${spec}`);
  const out = [];
  for (let i = from; i <= to; i++) out.push(String(i).padStart(a.length, '0'));
  return out;
}

function resolveTargets(opts) {
  const nameFor = () => (opts.asCover ? `${crypto.randomUUID()}.png` : '封面_chatgpt.png');

  if (opts.root || opts.dirs || opts.all) {
    const root = opts.root ? expand(opts.root) : WORKSPACE_DIR;
    if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) die(`根目录不存在：${root}`);
    let names;
    if (opts.dirs?.length) names = opts.dirs.flatMap(expandDirSpec);
    else if (opts.all) {
      names = fs.readdirSync(root).filter((d) => {
        try { return fs.statSync(path.join(root, d)).isDirectory(); } catch { return false; }
      }).sort();
    } else die('批量模式需要 --dirs <目录名列表> 或 --all');
    if (opts.dirs?.length) names = names.flatMap((n) => (String(n).includes(',') ? String(n).split(',') : [n]));

    const targets = [], skipped = [];
    for (const n of names) {
      const dir = path.isAbsolute(n) ? n : path.join(root, n);
      const name = path.basename(dir);
      if (!fs.existsSync(dir)) { skipped.push(`${name}（目录不存在）`); continue; }
      const img = ['封面.jpg', '封面.png'].map((f) => path.join(dir, f)).find((f) => fs.existsSync(f));
      if (!img) { skipped.push(`${name}（没有 封面.jpg 或 封面.png）`); continue; }
      const out = opts.out ? opts.out : path.join(dir, nameFor());
      if (opts.skipExisting && fs.existsSync(out)) { skipped.push(`${name}（已有 ${path.basename(out)}）`); continue; }
      targets.push({ dir, name, image: img, out });
    }
    log('根目录', root);
    if (skipped.length) {
      const shown = skipped.slice(0, 5).join('；');
      log('跳过', skipped.length > 5 ? `${shown} 等 ${skipped.length} 个` : shown);
    }
    RESOLVE_SKIPPED = skipped;
    if (!targets.length) die('没有可处理的目录');
    return targets;
  }

  // 单张模式
  if (opts.input) {
    const one = resolveInput(opts.input);
    const dir = path.dirname(one);
    return [{ dir, name: path.basename(dir), image: one, out: opts.out ?? path.join(dir, nameFor()) }];
  }
  die('请给出日期目录/图片路径，或用 --root + --dirs 指定批量目标（--help 查看用法）');
}

const targets = resolveTargets(opts);
if (opts.plan) {
  console.log(`\n共 ${targets.length} 个目标：`);
  for (const [i, t] of targets.entries()) {
    console.log(`  ${i + 1}. [${t.name}] ${t.image}`);
    console.log(`     → ${t.out}`);
  }
  process.exit(0);
}

log('队列', profiles.map((p) => `${p.name}(:${p.port})`).join(' → '));
console.log(`共 ${targets.length} 个目标目录，开始处理\n`);

const summary = [];
let live = profiles.slice();     // 额度耗尽的账号本轮内不再尝试
let session = null;

for (const [ti, t] of targets.entries()) {
  const head = `[${ti + 1}/${targets.length}] ${t.name}`;
  opts.image = t.image;
  opts.out = t.out;
  console.log(`──── ${head}  ${t.image}`);
  if (!live.length) { summary.push({ ...t, status: 'no-account' }); continue; }

  let done = false;
  for (const p of live) {
    const r = await runProfile(p, opts);
    if (r.session) session = r.session;

    if (r.status === 'ok' || r.status === 'done-dry') {
      for (const it of r.saved ?? []) {
        console.log(`  ✓ ${it.finalPath}  ${it.w}x${it.h} (via ${it.via})`);
        if (opts.fit && it.rawPath !== it.finalPath) console.log(`    原始出图: ${it.rawPath}`);
      }
      summary.push({ ...t, status: r.status, saved: r.saved });
      done = true;
      break;
    }
    if (r.status === 'quota') {
      st.exhausted = st.exhausted ?? {};
      st.exhausted[p.name] = new Date().toISOString();
      saveState(st);
      log(p.name, '额度耗尽 → 已记录，切换下一个账号');
      live = live.filter((x) => x.name !== p.name);
      continue;
    }
    log(p.name, `本轮未成功（${r.status}${r.text ? '：' + r.text.replace(/\s+/g, ' ').slice(0, 120) : ''}），切换下一个账号`);
  }
  if (!done) summary.push({ ...t, status: 'failed' });
  if (!live.length) console.log('  （所有账号额度已耗尽，剩余目录将全部跳过）');
}

await teardown(session, opts);

// ───────────────────────── 汇总 ─────────────────────────
const okList = summary.filter((s) => s.status === 'ok' || s.status === 'done-dry');
const otherList = summary.filter((s) => !(s.status === 'ok' || s.status === 'done-dry'));
console.log('\n════════ 汇总 ════════');
for (const s of okList) {
  const f = s.saved?.[0];
  console.log(`  ✓ ${s.name}  ${f ? `${f.finalPath}  ${f.w}x${f.h}` : '(dry-run，未发送)'}`);
}
for (const s of otherList) {
  const why = { 'no-account': '所有账号额度耗尽', failed: '失败' }[s.status] ?? s.status;
  console.log(`  ✗ ${s.name}  ${why}`);
}
console.log(`\n成功 ${okList.length} / 共 ${summary.length}`);
if (otherList.length) {
  console.log('失败或跳过的目录可以直接重跑同一命令（已成功的加 --skip-existing 可跳过）');
}
process.exit(otherList.length ? 2 : 0);
