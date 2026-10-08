/* eslint-disable */
/**
 * Node 端自检：把网页 Agent 里的 JS 抽出来真的执行一遍。
 *
 * 为什么必须做这一步
 * ------------------
 * 网页 JS 在浏览器里没跑过就不能算完成。这个脚本把 HTML 里的
 * 业务脚本抽出、在 Node 沙箱里用桩件（stub）代替 DOM 与 Blob，
 * 然后对每个操作跑一遍真实数据，逐项校验产出结构。
 * 它抓的是「操作函数本身的逻辑错误」，这是最容易出错也最该自动化的部分。
 *
 * 用法： node verify_agent.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PROJ = path.resolve(__dirname, '..', '..');
const HTML = path.join(PROJ, 'results', '运营问数Agent.html');
const DATA_JS = path.join(PROJ, 'tools', 'web_data.js');

let failures = [];
let checks = 0;
function check(name, cond, detail) {
  checks++;
  if (cond) {
    console.log(`  [OK  ] ${name}`);
  } else {
    console.log(`  [FAIL] ${name}${detail ? '  -> ' + detail : ''}`);
    failures.push(name);
  }
}

console.log('='.repeat(74));
console.log(' 网页 Agent JS 自检（在 Node 沙箱里真实执行）');
console.log('='.repeat(74));

// ---------------------------------------------------------------- 抽取脚本
// 注意：不能用非贪婪正则 /<script>([\s\S]*?)<\/script>/ 来切块——
// 代码里出现 `i<bin.length` 这类比较表达式时，非贪婪匹配会在第一个 "</"
// 序列处提前截断，把好文件误判成语法错误（我自己先踩了这个坑）。
// 正确做法是顺序扫描：从前一个 </script> 之后找下一个 <script>。
function splitScripts(src) {
  const out = [];
  let pos = 0;
  for (;;) {
    const i = src.indexOf('<script>', pos);
    if (i < 0) break;
    const j = src.indexOf('</script>', i);
    if (j < 0) break;
    out.push(src.slice(i + 8, j));
    pos = j + 9;
  }
  return out;
}

const html = fs.readFileSync(HTML, 'utf8');
const scripts = splitScripts(html);
console.log(`\n内联 <script> 块数: ${scripts.length}`);

// 逐块语法检查（用 vm.Script 只编译不执行）
let syntaxBad = 0;
scripts.forEach((s, i) => {
  try {
    new vm.Script(s, {filename: `blk${i}.js`});
  } catch (e) {
    syntaxBad++;
    console.log(`  [FAIL] 块 ${i} 语法错误: ${e.message}`);
  }
});
check(`全部 ${scripts.length} 个脚本块语法正确`, syntaxBad === 0);

// 最后一块是 init（依赖 DOM），前面的是数据/工具/操作/流水线/渲染
const codeBlocks = scripts.slice(0, -1);

// ---------------------------------------------------------------- 沙箱环境
const sandbox = {
  console,
  performance: { now: () => Number(process.hrtime.bigint() / 1000000n) },
  setTimeout, clearTimeout,
  Blob: class { constructor(parts) { this.size = (parts || []).reduce((s, p) => s + (p.length || 0), 0); } },
  URL: { createObjectURL: () => 'blob:stub' },
  atob: s => Buffer.from(s, 'base64').toString('binary'),
  DecompressionStream: undefined,
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  fetch: () => Promise.reject(new Error('stub: no network in sandbox')),
  XLSX: null,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

// 注入数据（未压缩版本）
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(DATA_JS, 'utf8'), sandbox, { filename: 'web_data.js' });

// 注入业务脚本（跳过最后一块 init），并在末尾追加导出代码。
//
// 为什么要「追加」而不是分块执行：
// 脚本里用的是 `const OPS = [...]`、`const ROWS = ...` 这类**块级声明**，
// 在 vm 里分块执行时它们不会挂到全局对象上，取不到。
// 把导出语句拼进同一段脚本，就能在同一作用域内把它们交出来。
// （这一点前后踩了两次：先用非贪婪正则切块导致假语法错，
//   再因块级作用域取不到变量。）
const EXPORTS = `
;globalThis.__export = {
  OPS, ROWS, WDMAP, D,
  matchByRules, extractParams, buildWorkbookSpec,
  METRIC_META, WAIT_IMPLAUSIBLE,
  mean, median, weekKey,
};
`;
const joined = codeBlocks.join('\n;\n') + EXPORTS;
try {
  vm.runInContext(joined, sandbox, {filename: 'agent_bundle.js'});
} catch (e) {
  console.log(`  [FAIL] 业务脚本执行报错: ${e.message}`);
  console.log((e.stack || '').split('\n').slice(0, 4).join('\n'));
  failures.push('bundle');
}
const X = sandbox.__export || {};

console.log('\n-- 数据加载 --');
check('window.OPS_DATA 存在', !!sandbox.OPS_DATA);
const D = X.D || sandbox.OPS_DATA;
if (!D) { console.log('\n无法继续：数据未加载'); process.exit(1); }
check(`网点数 = ${D.meta.n_branches}`, D.meta.n_branches === 20);
check(`明细行数 = ${D.meta.n_rows}`, D.meta.n_rows === 1800);
check(`日期数 = ${D.meta.n_dates}`, D.meta.n_dates === 90);
check(`预警数 = ${D.alerts.length}`, D.alerts.length > 0);
check('列式数据长度一致',
  D.detail_cols.date_i.length === D.detail_cols.vol.length &&
  D.detail_cols.vol.length === D.meta.n_rows);

console.log('\n-- 操作白名单 --');
const OPS = X.OPS;
check('OPS 已定义', Array.isArray(OPS));
check(`操作数量 = ${OPS ? OPS.length : 0}（≥6）`, OPS && OPS.length >= 6);
if (OPS) {
  OPS.forEach(op => {
    check(`  ${op.id} 具备 run/title/keywords`,
      typeof op.run === 'function' && !!op.title && Array.isArray(op.keywords));
  });
}

// ---------------------------------------------------------------- 逐个执行
console.log('\n-- 逐操作执行（真实数据） --');
function runOp(id, params) {
  const op = OPS.find(o => o.id === id);
  if (!op) throw new Error('未找到操作 ' + id);
  return op.run(params);
}

const CASES = [
  ['branch_rank', {metric: 'wait', n: 8, order: 'desc'}, '网点排名（等候时长）'],
  ['branch_rank', {metric: 'err', n: 5, order: 'desc'}, '网点排名（差错率）'],
  ['weekday_pattern', {metric: 'vol'}, '周内规律'],
  ['trend', {metric: 'wait', from: null, to: null, grain: 'day'}, '趋势（日度）'],
  ['trend', {metric: 'vol', from: null, to: null, grain: 'week'}, '趋势（周度）'],
  ['branch_drill', {branch: 'SH010'}, '网点深钻'],
  ['alert_analysis', {view: 'cause'}, '预警分析（按成因）'],
  ['alert_analysis', {view: 'severity'}, '预警分析（按严重度）'],
  ['data_quality', {}, '数据质量核查'],
];

const outputs = {};
CASES.forEach(([id, params, label]) => {
  try {
    const out = runOp(id, params);
    outputs[id] = out;
    const okShape = out && Array.isArray(out.sheets) && Array.isArray(out.kpis) &&
                    Array.isArray(out.charts) && typeof out.interpretation === 'string';
    const rowsN = out.sheets.reduce((s, x) => s + (x.data ? x.data.length : 0), 0);
    check(`${label}：结构完整（${out.sheets.length} 表 / ${rowsN} 行 / ${out.charts.length} 图）`,
      okShape && rowsN > 0 && out.interpretation.length > 20);
  } catch (e) {
    check(`${label}：执行`, false, e.message + ' @ ' + (e.stack || '').split('\n')[1]);
  }
});

// ---------------------------------------------------------------- 数值校验
console.log('\n-- 关键数值正确性 --');

// 1) 周内效应：周日业务量应显著低于周一（这是整个项目的方法论基石）
try {
  const wp = runOp('weekday_pattern', {metric: 'vol'});
  const sheet = wp.sheets[0];
  const mon = sheet.data.find(x => x.name === '周一').vol;
  const sun = sheet.data.find(x => x.name === '周日').vol;
  const ratio = sun / mon;
  check(`周日/周一业务量比 = ${ratio.toFixed(3)}（应 < 0.5，验证周内效应存在）`,
    ratio < 0.5 && ratio > 0.1);
  // 图表的 KPI 应该把这个落差展示出来
  const kpi = wp.kpis.find(k => k.k === '落差');
  check(`周内落差 KPI = ${kpi ? kpi.v : '缺失'}`, !!kpi && /%$/.test(kpi.v));
} catch (e) { check('周内规律数值校验', false, e.message); }

// 2) 网点排名：降序应单调不增
try {
  const br = runOp('branch_rank', {metric: 'wait', n: 20, order: 'desc'});
  const vals = br.sheets[0].data.map(x => x.v);
  const sorted = vals.slice().sort((a, b) => b - a);
  check('排名降序单调不增', JSON.stringify(vals) === JSON.stringify(sorted));
  const kpiMax = br.kpis.find(k => k.k.includes('最高'));
  // 容差取显示精度的一半：KPI 与表格都按 meta.dec 显示（等候时长 1 位小数），
  // 因此两者显示的字符串应完全一致；这里断言的是显示值，容差 0.05 足够。
  check(`最高值 KPI 与首行一致（${kpiMax.v} vs ${vals[0].toFixed(1)}）`,
    Math.abs(parseFloat(kpiMax.v.replace(/,/g, '')) - vals[0]) < 0.05);
} catch (e) { check('网点排名数值校验', false, e.message); }

// 3) 数据质量：单位错误记录数应 > 0，且等候时长都 > 180
try {
  const dq = runOp('data_quality', {});
  const bad = dq.sheets[0].data;
  check(`单位错误记录数 = ${bad.length}（应 > 0）`, bad.length > 0);
  check('全部超过 180 分钟阈值', bad.every(x => x.wait > 180));
  check('倍数均 > 3', bad.every(x => x.times === null || x.times > 3));
} catch (e) { check('数据质量数值校验', false, e.message); }

// 4) 网点深钻：该网点预警数应与全局一致
try {
  const dr = runOp('branch_drill', {branch: 'SH010'});
  const inSheet = dr.sheets.find(s => s.name === '该网点预警').data.length;
  const global = D.alerts.filter(a => a.code === 'SH010').length;
  check(`SH010 预警数一致（表内 ${inSheet} / 全局 ${global}）`, inSheet === global);
  const drill = dr.sheets[0].data;
  check('画像表含数据性质相关条目', drill.length >= 12);
} catch (e) { check('网点深钻数值校验', false, e.message); }

// 5) 趋势：数据点数应等于区间天数
try {
  const tr = runOp('trend', {metric: 'vol', from: null, to: null, grain: 'day'});
  check(`趋势数据点 = ${tr.sheets[0].data.length}（应 = ${D.meta.n_dates}）`,
    tr.sheets[0].data.length === D.meta.n_dates);
} catch (e) { check('趋势数值校验', false, e.message); }

// ---------------------------------------------------------------- 意图路由
console.log('\n-- 意图路由（模板匹配） --');
const matchByRules = X.matchByRules;
const extractParams = X.extractParams;
if (typeof matchByRules === 'function') {
  const ROUTE_CASES = [
    ['哪些网点等候时长最长', 'branch_rank'],
    ['周一到周日业务量怎么变化的', 'weekday_pattern'],
    ['8 月相比 7 月业务量有没有异常', 'trend'],
    ['看一下 SH010 这个网点的详细情况', 'branch_drill'],
    ['一共报了多少条预警，都是什么性质', 'alert_analysis'],
    ['数据里有多少可疑的单位错误记录', 'data_quality'],
    ['各网点业务差错率排名', 'branch_rank'],
  ];
  ROUTE_CASES.forEach(([q, expect]) => {
    const m = matchByRules(q);
    const got = m ? m.op.id : '(未匹配)';
    check(`「${q}」→ ${got}`, got === expect, `期望 ${expect}`);
  });

  // 参数抽取
  const ex = extractParams(OPS.find(o => o.id === 'branch_drill'), '看一下 SH010 这个网点');
  check(`参数抽取网点 = ${ex.params.branch}`, ex.params.branch === 'SH010');
  const ex2 = extractParams(OPS.find(o => o.id === 'branch_rank'), '差错率最高的 5 家网点');
  check(`参数抽取 metric=${ex2.params.metric} n=${ex2.params.n}`,
    ex2.params.metric === 'err' && ex2.params.n === 5);
  const ex3 = extractParams(OPS.find(o => o.id === 'trend'), '8 月业务量趋势');
  check(`参数抽取日期区间 = ${ex3.params.from} ~ ${ex3.params.to}`,
    ex3.params.from && ex3.params.from.startsWith('2026-08') &&
    ex3.params.to && ex3.params.to.startsWith('2026-08'));
} else {
  check('matchByRules 可用', false);
}

// ---------------------------------------------------------------- 工作簿规格
console.log('\n-- 工作簿规格构建 --');
const buildSpec = X.buildWorkbookSpec;
if (typeof buildSpec === 'function') {
  try {
    const out = outputs['branch_rank'];
    const spec = buildSpec(OPS.find(o => o.id === 'branch_rank'),
      {metric: 'wait', n: 8, order: 'desc'}, out, '哪些网点等候时长最长');
    check('文件名为 .xlsx', /\.xlsx$/.test(spec.filename), spec.filename);
    check('第 1 张表是「说明与数据性质」', spec.sheets[0].name === '说明与数据性质');
    const first = spec.sheets[0].rows.map(r => r.join('|')).join(' ');
    check('声明里含「模拟数据」字样', first.includes('模拟'));
    check('声明里含「不代表任何真实银行」', first.includes('不代表任何真实银行'));
    const dataSheets = spec.sheets.slice(2);
    check(`数据表均含表头与行（${dataSheets.length} 张）`,
      dataSheets.every(s => Array.isArray(s.header) && s.rows.length > 0));
    check('列宽已设置', spec.sheets.every(s => Array.isArray(s.colWidths) && s.colWidths.length > 0));
  } catch (e) {
    check('工作簿规格构建', false, e.message);
  }
} else check('buildWorkbookSpec 可用', false);

// ---------------------------------------------------------------- 汇总
console.log('\n' + '='.repeat(74));
if (failures.length === 0) {
  console.log(` 全部通过：${checks} 项检查`);
  process.exit(0);
} else {
  console.log(` 通过 ${checks - failures.length}/${checks}，失败 ${failures.length} 项：`);
  failures.forEach(f => console.log('   - ' + f));
  process.exit(1);
}
