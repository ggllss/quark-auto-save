#!/usr/bin/env node
/**
 * 前端（app/templates/index.html 内联 Vue 组件）回归测试。
 *
 * 运行方式（需要 node，无需浏览器与服务器）：
 *   node tests/test_frontend.js
 *
 * 做法：抽取模板里的内联脚本，在 Node 中 stub 掉 Vue/axios/jQuery 后执行，
 * 拿到真实的组件选项对象，再驱动其中的方法，验证行为而不是语法。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.dirname(__dirname);
const TEMPLATE = path.join(ROOT, 'app', 'templates', 'index.html');
const html = fs.readFileSync(TEMPLATE, 'utf8');

let passed = 0;
let failed = 0;

function check(name, cond, extra) {
  if (cond) {
    console.log('  \u2705 ' + name);
    passed++;
  } else {
    console.log('  \u274c ' + name + (extra !== undefined ? '  \u2192 ' + JSON.stringify(extra) : ''));
    failed++;
  }
}

/** 按花括号配对抽出一个方法定义 */
function extractMethod(src, name) {
  const i = src.indexOf(name + '() {');
  if (i < 0) throw new Error('方法未找到: ' + name);
  let depth = 0;
  for (let j = src.indexOf('{', i); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') {
      depth--;
      if (depth === 0) return src.slice(i, j + 1);
    }
  }
  throw new Error('花括号不配对: ' + name);
}

// ---------------------------------------------------------------- 载入组件
const TAG = '\n  <script>\n';
const scriptStart = html.indexOf(TAG);
if (scriptStart < 0) {
  console.error('未找到内联 <script> 块');
  process.exit(1);
}
const inlineJs = html.slice(scriptStart + TAG.length, html.indexOf('\n  </script>', scriptStart));

let options = null;
global.Vue = function (opts) { options = opts; };
global.Vue.set = function (o, k, v) { o[k] = v; };
global.$ = () => ({ modal() {}, collapse() {}, tooltip() {}, on() {} });
global.document = { addEventListener() {}, removeEventListener() {} };
global.window = { addEventListener() {}, removeEventListener() {}, innerWidth: 1200 };
// Node 21+ 内置了只读的 global.navigator，直接赋值在严格模式下会抛错
try {
  Object.defineProperty(global, 'navigator', {
    value: { userAgent: 'node-test' }, configurable: true, writable: true
  });
} catch (e) {
  /* 无法覆盖时沿用 Node 自带实现即可 */
}

const calls = [];
global.axios = {
  get(url) {
    calls.push(['GET', url]);
    return Promise.resolve(global.__nextGet || { data: { success: true, data: {} } });
  },
  post(url, body) {
    calls.push(['POST', url, body]);
    return Promise.resolve(global.__nextResponse || { data: { success: false } });
  }
};

// 用 new Function 执行：其函数体默认是「宽松模式」，与浏览器的行为一致。
// 上游模板大量依赖隐式全局（config_data / parentDir / dir / params 等都未声明），
// 若用严格模式的 eval 执行会直接抛 ReferenceError。
new Function(inlineJs)();
if (!options) {
  console.error('未能捕获 Vue 组件（脚本执行失败）');
  process.exit(1);
}

const ctx = Object.assign({}, JSON.parse(JSON.stringify(options.data)), options.methods);
ctx.showToast = function () {};
ctx.$set = function (o, k, v) { o[k] = v; };
ctx.$nextTick = function (fn) { fn(); };
const tick = () => new Promise(r => setTimeout(r, 5));

// -------------------------------------------------------------------- 用例
async function testComponent() {
  console.log('【1】组件加载与状态初始化');
  check('捕获到 Vue 组件', !!options);
  check('aiForm 已初始化', !!ctx.aiForm && typeof ctx.aiForm.keyword === 'string');
  check('formData.ai 有默认值（模板绑定不会踩 undefined）', !!ctx.formData.ai, ctx.formData.ai);
  check('fileSelect 有默认值', !!ctx.fileSelect && Array.isArray(ctx.fileSelect.selectedFids));
}

function testHandlers() {
  console.log('【2】模板中调用的方法都已定义');
  const templateOnly = html.slice(0, scriptStart);
  const defined = new Set();
  for (const m of inlineJs.matchAll(/^        (?:async\s+)?([A-Za-z_$][\w$]*)\s*\(/gm)) defined.add(m[1]);
  for (const m of inlineJs.matchAll(/^          ([A-Za-z_$][\w$]*)\s*:/gm)) defined.add(m[1]);

  const builtins = new Set(['$set', '$delete', '$emit', 'parseInt', 'parseFloat', 'String',
    'Number', 'Boolean', 'JSON', 'Math', 'Date', 'Array', 'Object', 'alert', 'confirm',
    'prompt', 'console', 'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof',
    'function', '$event']);
  const missing = [];
  for (const m of templateOnly.matchAll(/(?:@|v-on:)[\w.]+="([^"]*)"/g)) {
    for (const c of m[1].matchAll(/([A-Za-z_$][\w$]*)\s*\(/g)) {
      if (!defined.has(c[1]) && !builtins.has(c[1]) && !missing.includes(c[1])) missing.push(c[1]);
    }
  }
  check('无未定义的事件处理器', missing.length === 0, missing);
}

async function testConfirmManualSelect() {
  console.log('【3】confirmManualSelect：分享链接回写（历史 bug 回归）');
  global.$ = () => ({ modal() {} });
  const methods = eval('({' + extractMethod(inlineJs, 'confirmManualSelect') + '})');

  function makeCtx(task, fileSelect) {
    return Object.assign({
      fileSelect: Object.assign({ index: 0, selectedFids: [], shareurl: '' }, fileSelect),
      formData: { tasklist: [task] },
      $set: (o, k, v) => { o[k] = v; }
    }, methods);
  }

  const changed = { shareurl: 'OLD', startfid: 'old_fid', fid_list: ['a'], shareurl_ban: 'err' };
  makeCtx(changed, { shareurl: 'NEW', selectedFids: ['x', 'y'] }).confirmManualSelect();
  check('换链接后任务链接同步更新', changed.shareurl === 'NEW', changed.shareurl);
  check('shareurl_ban 被清除', changed.shareurl_ban === undefined);
  check('旧链接的 startfid 被清空', changed.startfid === undefined);
  check('fid_list 更新为本次勾选', JSON.stringify(changed.fid_list) === JSON.stringify(['x', 'y']));

  const same = { shareurl: 'SAME', startfid: 'keep', fid_list: ['a'] };
  makeCtx(same, { shareurl: 'SAME', selectedFids: ['x'] }).confirmManualSelect();
  check('链接未变时不动 startfid', same.startfid === 'keep', same.startfid);
  check('链接未变时仍更新 fid_list', JSON.stringify(same.fid_list) === JSON.stringify(['x']));

  const blank = { shareurl: 'OLD2', startfid: 'keep' };
  makeCtx(blank, { shareurl: '', selectedFids: ['z'] }).confirmManualSelect();
  check('shareurl 为空时不覆盖任务链接', blank.shareurl === 'OLD2', blank.shareurl);
}

function testSelectAll() {
  console.log('【4】转存内容勾选的默认全选与全选框');
  const methods = eval('({' + extractMethod(inlineJs, 'preselectTransferFiles') + ','
    + extractMethod(inlineJs, 'toggleSelectAll') + '})');
  const list = [{ fid: 'a' }, { fid: 'b' }, { fid: 'c' }];

  function makeCtx(savedFids) {
    return Object.assign({
      fileSelect: { fileList: list, selectedFids: [], index: 0 },
      formData: { tasklist: [{ fid_list: savedFids }] }
    }, methods);
  }

  const fresh = makeCtx(undefined);
  fresh.preselectTransferFiles();
  check('首次打开默认全选', fresh.fileSelect.selectedFids.length === 3);
  fresh.toggleSelectAll();
  check('点全选框可取消全选', fresh.fileSelect.selectedFids.length === 0);
  fresh.toggleSelectAll();
  check('再点可恢复全选', fresh.fileSelect.selectedFids.length === 3);

  const saved = makeCtx(['a', 'c']);
  saved.preselectTransferFiles();
  check('已保存过勾选时沿用保存值',
    JSON.stringify(saved.fileSelect.selectedFids) === JSON.stringify(['a', 'c']),
    saved.fileSelect.selectedFids);

  const partial = makeCtx(undefined);
  partial.preselectTransferFiles();
  partial.fileSelect.selectedFids = partial.fileSelect.selectedFids.filter(id => id !== 'b');
  partial.toggleSelectAll();
  check('部分勾选后点全选框补齐', partial.fileSelect.selectedFids.length === 3);
}

async function testAiFlow() {
  console.log('【5】AI 追剧流程');
  check('AI 方法齐全',
    ['aiSearch', 'aiAnalyze', 'aiCommit', 'aiToggleAll', 'aiSelectedCount',
      'aiApplyPreset', 'aiTestConnection'].every(m => typeof ctx[m] === 'function'));

  ctx.aiForm.keyword = '   ';
  calls.length = 0;
  ctx.aiSearch();
  await tick();
  check('空输入不发请求', calls.filter(c => c[1] === '/ai/search').length === 0);

  calls.length = 0;
  ctx.aiForm.keyword = '背着善宰跑';
  global.__nextResponse = {
    data: {
      success: true, data: {
        intent: { title: '背着善宰跑', season: 1, queries: ['背着善宰跑'] },
        ai_used: false,
        candidates: [{ taskname: '资源A', shareurl: 'https://pan.quark.cn/s/aaa', score: 90 }],
        baseline: { taskname: '背着善宰跑', savepath: '/0-影音/剧/背着善宰跑', pattern: '$TV_MAGIC', replace: '', match_reason: '剧名完全一致' }
      }
    }
  };
  ctx.aiSearch();
  await tick();
  check('调用 /ai/search', calls.some(c => c[1] === '/ai/search'));
  check('按已有任务预填保存路径', ctx.aiForm.savepath === '/0-影音/剧/背着善宰跑', ctx.aiForm.savepath);
  check('按已有任务预填命名规则', ctx.aiForm.pattern === '$TV_MAGIC', ctx.aiForm.pattern);

  calls.length = 0;
  global.__nextResponse = {
    data: {
      success: true, data: {
        rows: [
          { fid: 'f1', file_name: 'A.E01.mp4', dir: false, status: 'new', status_text: '可新增', new_name: '剧.S01E01.mp4', default_selected: true },
          { fid: 'f2', file_name: 'A.E02.mp4', dir: false, status: 'dup_episode', status_text: '疑似重复', new_name: '剧.S01E02.mp4', default_selected: false },
          { fid: 'f3', file_name: 'A.E03.mp4', dir: false, status: 'exists', status_text: '已在库', new_name: '剧.S01E03.mp4', default_selected: false }
        ],
        stats: { total: 3, new: 1, exists: 1, dup_episode: 1, unmatched: 0, folder: 0 },
        savepath: '/x', savepath_exists: true, dir_note: '', dir_file_count: 3,
        rule: { pattern: '$TV_MAGIC', replace: '', valid: true }, dir_files: []
      }
    }
  };
  ctx.aiSelectLink({ shareurl: 'https://pan.quark.cn/s/aaa', taskname: '背着善宰跑' });
  await tick();
  check('调用 /ai/analyze', calls.some(c => c[1] === '/ai/analyze'));
  check('默认只勾选「可新增」',
    ctx.aiAnalysis.rows[0].selected === true &&
    ctx.aiAnalysis.rows[1].selected === false &&
    ctx.aiAnalysis.rows[2].selected === false);
  check('已选计数正确', ctx.aiSelectedCount() === 1);
  check('「已在库」项不可选', ctx.aiSelectable(ctx.aiAnalysis.rows[2]) === false);

  ctx.aiToggleAll({ target: { checked: true } });
  check('全选只勾可选条目（跳过已在库）',
    ctx.aiSelectedCount() === 2 && ctx.aiAnalysis.rows[2].selected === false, ctx.aiSelectedCount());

  calls.length = 0;
  ctx.aiAnalysis.rows[0].new_name = '手动改名.S01E01.mp4';
  global.__nextResponse = { data: { success: true, data: { ok: true, log: '转存完成' } } };
  ctx.aiCommit();
  await tick();
  const commitCall = calls.find(c => c[1] === '/ai/commit');
  check('调用 /ai/commit 且提交选中条目', !!commitCall && commitCall[2].files.length === 2);
  check('手动改的名字被带进请求体',
    commitCall[2].files.some(x => x.new_name === '手动改名.S01E01.mp4'));
  check('入库日志回显', ctx.aiCommitLog === '转存完成', ctx.aiCommitLog);

  calls.length = 0;
  ctx.formData.magic_regex = { '$TV_MAGIC': { pattern: '.*\\.(mp4)$', replace: '{TASKNAME}.{SXX}E{E}.{EXT}' } };
  ctx.aiForm.pattern = '$TV_MAGIC';
  global.__nextResponse = { data: { success: true, data: { rows: [], stats: {}, rule: { valid: true }, dir_files: [] } } };
  ctx.aiApplyPreset();
  await tick();
  check('选预设带出替换模板', ctx.aiForm.replace === '{TASKNAME}.{SXX}E{E}.{EXT}', ctx.aiForm.replace);
  check('选预设触发重新分析', calls.some(c => c[1] === '/ai/analyze'));
}

async function testFetchDataNormalize() {
  console.log('【6】fetchData：AI 配置归一化与 Key 掩码');
  global.__nextGet = {
    data: {
      success: true, data: {
        cookie: [], tasklist: [], magic_regex: {}, source: {},
        ai: { enable: true, base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat', api_key: '', api_key_set: true }
      }
    }
  };
  ctx.fetchData();
  await tick();
  check('布尔 true 归一化为字符串 "true"', ctx.formData.ai.enable === 'true', ctx.formData.ai.enable);
  check('api_key 回传为空（已掩码）', ctx.formData.ai.api_key === '', ctx.formData.ai.api_key);
  check('api_key_set 透传到前端', ctx.formData.ai.api_key_set === true);

  global.__nextGet = { data: { success: true, data: { cookie: [], tasklist: [], magic_regex: {}, source: {}, ai: { enable: false } } } };
  ctx.fetchData();
  await tick();
  check('布尔 false 归一化为 "false"', ctx.formData.ai.enable === 'false', ctx.formData.ai.enable);
  check('缺字段补默认值', ctx.formData.ai.model === '' && ctx.formData.ai.base_url === '');
}

async function testPathResolution() {
  console.log('【7】类型判定与入库路径解析');
  check('路径解析方法已定义',
    typeof ctx.aiResolvePath === 'function' && typeof ctx.aiApplyResolved === 'function');

  ctx.aiApplyResolved({
    category: '综艺', category_source: 'existing_folder', season: 4,
    matched_folder: '现在就出发', folder_exists: true,
    season_dir: 'S04', season_exists: false,
    savepath: '/0-影音/综艺/现在就出发/S04', create_dirs: [], note: '命中已有文件夹',
    category_options: ['电视剧', '电影', '动漫', '综艺', '纪录片']
  });
  check('应用判定出的类型', ctx.aiForm.category === '综艺', ctx.aiForm.category);
  check('应用季号', ctx.aiForm.season === 4, ctx.aiForm.season);
  check('应用入库路径', ctx.aiForm.savepath === '/0-影音/综艺/现在就出发/S04', ctx.aiForm.savepath);
  check('记录「已存在」状态', ctx.aiResolved && ctx.aiResolved.folder_exists === true);

  calls.length = 0;
  global.__nextResponse = {
    data: {
      success: true, data: {
        category: '电视剧', category_source: 'manual', season: 4, matched_folder: null,
        folder_exists: false, season_dir: 'S04', season_exists: false,
        savepath: '/0-影音/电视剧/现在就出发/S04',
        create_dirs: ['/0-影音/电视剧/现在就出发', '/0-影音/电视剧/现在就出发/S04'],
        note: '将新建：/0-影音/电视剧/现在就出发/S04',
        category_options: ['电视剧', '电影', '动漫', '综艺', '纪录片']
      }
    }
  };
  ctx.aiForm.category = '电视剧';
  ctx.aiResolvePath();
  await tick();
  const rp = calls.find(c => c[1] === '/ai/resolve_path');
  check('切换类型会调用 /ai/resolve_path', !!rp);
  check('请求带上类型与季号', !!rp && rp[2].category === '电视剧' && rp[2].season === 4,
    rp && rp[2]);
  check('未命中时按新文件夹计算路径',
    ctx.aiForm.savepath === '/0-影音/电视剧/现在就出发/S04', ctx.aiForm.savepath);
  check('标记为「将新建」', ctx.aiResolved.folder_exists === false);
  check('给出要新建的层级', (ctx.aiResolved.create_dirs || []).length === 2,
    ctx.aiResolved.create_dirs);

  calls.length = 0;
  ctx.aiForm.keyword = '现在就出发 第四季';
  global.__nextResponse = {
    data: {
      success: true, data: {
        intent: { title: '现在就出发', season: 4, aliases: [], queries: ['现在就出发'], source: 'ai', category: '综艺' },
        ai_used: true,
        candidates: [{ taskname: 'x', shareurl: 'https://pan.quark.cn/s/x', score: 90 }],
        baseline: { taskname: '现在就出发', savepath: '/0-影音/综艺/现在就出发/S04', pattern: '$TV_MAGIC', replace: '', match_reason: '剧名完全一致' },
        resolved: {
          category: '综艺', category_source: 'existing_folder', season: 4,
          matched_folder: '现在就出发', folder_exists: true,
          season_dir: 'S04', season_exists: true,
          savepath: '/0-影音/综艺/现在就出发/S04', create_dirs: [], note: '命中已有文件夹「现在就出发」'
        }
      }
    }
  };
  ctx.aiSearch();
  await tick();
  check('搜索后自动应用入库路径',
    ctx.aiForm.savepath === '/0-影音/综艺/现在就出发/S04', ctx.aiForm.savepath);
  check('搜索后自动填充类型', ctx.aiForm.category === '综艺', ctx.aiForm.category);
  check('搜索后自动填充季号', ctx.aiForm.season === 4, ctx.aiForm.season);
  check('与已有任务路径一致时不提示', ctx.aiBaselinePath === '', ctx.aiBaselinePath);
}

function testMobileMarkup() {
  console.log('【8】手机端适配');
  const trCount = (html.match(/table-responsive/g) || []).length;
  check('所有表格都支持横向滚动', trCount >= 2, trCount);
  check('分析结果有手机端卡片视图', /d-md-none[\s\S]{0,900}aiAnalysis\.rows/.test(html));
  check('分析表格仅在中屏以上显示', html.includes('d-none d-md-block table-responsive'));
  // 候选列表改成卡片后不再用表格，手机上不会横向溢出
  check('候选列表不是表格（手机不溢出）',
    !/table[\s\S]{0,300}aiResult\.candidates/.test(html));
  check('操作按钮区支持换行', html.includes('d-flex flex-wrap align-items-center'));
  check('手机端有独立的全选控件', html.includes('aiSelectAllMobile'));
  check('类型/季号输入在手机上不被挤压',
    html.includes('col-6 col-md-2') && html.includes('col-12 col-md-4'));
  check('「已存在 / 将新建」徽标按是否需要新建层级判定',
    html.includes("(aiResolved.create_dirs || []).length ? '将新建文件夹' : '存入已有文件夹'"));
  check('类型目录可在系统配置里修改', html.includes('formData.ai.media_roots[t]'));
}

async function testMobileInteraction() {
  console.log('【9】手机端操作逻辑（不只是样式）');
  ctx.aiAnalysis = {
    rows: [
      { fid: 'a', file_name: 'A.E01.mp4', status: 'new', status_text: '可新增', new_name: 'x', selected: false },
      { fid: 'b', file_name: 'A.E02.mp4', status: 'dup_episode', status_text: '疑似重复', new_name: 'y', selected: false },
      { fid: 'c', file_name: 'A.E03.mp4', status: 'exists', status_text: '已在库', new_name: 'z', selected: false },
      { fid: 'd', file_name: 'A.E04.mp4', status: 'unmatched', status_text: '不匹配规则', new_name: 'w', selected: false }
    ],
    stats: { new: 1, exists: 1, dup_episode: 1, unmatched: 1, folder: 0 },
    dir_file_count: 3, rule: { valid: true }
  };

  ctx.aiFilter = 'all';
  check('「全部」返回所有行', ctx.aiFilteredRows().length === 4);
  ctx.aiFilter = 'new';
  check('「可新增」只返回可新增行',
    ctx.aiFilteredRows().length === 1 && ctx.aiFilteredRows()[0].status === 'new');
  ctx.aiFilter = 'dup';
  check('「疑似重复」只返回疑似重复行',
    ctx.aiFilteredRows().length === 1 && ctx.aiFilteredRows()[0].status === 'dup_episode');
  ctx.aiFilter = 'exists';
  check('「已在库」只返回已在库行',
    ctx.aiFilteredRows().length === 1 && ctx.aiFilteredRows()[0].status === 'exists');
  ctx.aiFilter = 'unmatched';
  check('「不匹配」返回不匹配/规则无效行', ctx.aiFilteredRows().length === 1);
  check('筛选按钮上的计数正确',
    ctx.aiFilterCount('new') === 1 && ctx.aiFilterCount('exists') === 1
    && ctx.aiFilterCount('unmatched') === 1 && ctx.aiFilterCount('all') === 4);

  // 第 1 条要求：没有「不可转存」这回事，不匹配的也能手动勾
  check('不匹配规则的行可以手动勾选', ctx.aiSelectable({ status: 'unmatched' }) === true);
  check('规则无效的行也可以手动勾选', ctx.aiSelectable({ status: 'rule_error' }) === true);
  check('已在库的行不参与全选', ctx.aiSelectable({ status: 'exists' }) === false);

  // 全选只作用于当前筛选可见的行（所见即所得）
  ctx.aiFilter = 'dup';
  ctx.aiToggleAll({ target: { checked: true } });
  check('全选只影响可见行',
    ctx.aiAnalysis.rows[1].selected === true && ctx.aiAnalysis.rows[0].selected === false,
    [ctx.aiAnalysis.rows[0].selected, ctx.aiAnalysis.rows[1].selected]);
  check('不可转存的行不受全选影响', ctx.aiAnalysis.rows[2].selected === false);

  // 一键只选可新增（手机上少点几次）
  ctx.aiFilter = 'all';
  ctx.aiAnalysis.rows.forEach(r => { r.selected = true; });
  ctx.aiSelectOnlyNew();
  check('「只选可新增」会清掉疑似重复的勾选',
    ctx.aiAnalysis.rows.filter(r => r.selected).length === 1 && ctx.aiAnalysis.rows[0].selected === true);

  // 选定链接后收起候选列表，避免把结果顶到很远
  ctx.aiResult = {
    candidates: [
      { taskname: '资源A', shareurl: 'https://pan.quark.cn/s/aaa', score: 90 },
      { taskname: '资源B', shareurl: 'https://pan.quark.cn/s/bbb', score: 80 },
      { taskname: '资源C', shareurl: 'https://pan.quark.cn/s/ccc', score: 70 }
    ]
  };
  ctx.aiCandidatesCollapsed = false;
  ctx.aiSelectedCandidate = null;
  ctx.aiCandidateIndex = -1;
  ctx.aiForm.taskname = 'X';
  global.__nextResponse = {
    data: { success: true, data: { rows: [], stats: {}, rule: { valid: true }, dir_files: [] } }
  };
  ctx.aiSelectLink(ctx.aiResult.candidates[0]);
  check('选定链接后自动收起候选列表', ctx.aiCandidatesCollapsed === true);
  check('记住已选候选用于摘要展示',
    !!ctx.aiSelectedCandidate && ctx.aiSelectedCandidate.taskname === '资源A');
  check('记住当前是第几条', ctx.aiCandidateIndex === 0);
  await tick();

  // 第 3 条要求：直接用上一个/下一个切换链接，不用回头重新选
  ctx.aiNextLink();
  check('下一个切到第 2 条',
    ctx.aiSelectedCandidate.taskname === '资源B' && ctx.aiCandidateIndex === 1);
  check('切换链接会更新分享地址', ctx.aiForm.shareurl === 'https://pan.quark.cn/s/bbb');
  await tick();
  ctx.aiPrevLink();
  check('上一个切回第 1 条',
    ctx.aiSelectedCandidate.taskname === '资源A' && ctx.aiCandidateIndex === 0);
  await tick();
  ctx.aiPrevLink();
  check('第 1 条时「上一个」不越界', ctx.aiCandidateIndex === 0);
  ctx.aiSelectIndex(2);
  check('可以直接跳到第 3 条', ctx.aiSelectedCandidate.taskname === '资源C');
  await tick();
  ctx.aiNextLink();
  check('最后一条时「下一个」不越界', ctx.aiCandidateIndex === 2);
  await tick();

  let scrollSafe = true;
  try { ctx.aiScrollToResults(); } catch (e) { scrollSafe = false; }
  check('无 DOM 环境下滚动逻辑不抛错', scrollSafe);

  console.log('【10】手机端交互所需的页面结构');
  check('底部有常驻操作条', html.includes('ai-actionbar'));
  check('入库设置在手机上可折叠、桌面常开',
    html.includes('ai-settings-collapsible')
    && html.includes('.ai-settings-collapsible { display: block !important; }'));
  check('状态筛选按钮组存在', html.includes('ai-filter-chips') && html.includes("aiFilter = 'new'"));
  check('列表按筛选结果渲染', html.includes('v-for="(row, ri) in aiFilteredRows()"'));
  check('分析区有滚动锚点', html.includes('id="aiAnalysisAnchor"'));
  check('手机端底部留白，避免被操作条遮挡', html.includes('ai-has-actionbar'));
  check('滚动到结果区时避开 sticky 导航',
    html.includes('#aiAnalysisAnchor') && html.includes('scroll-margin-top: 64px'));
  check('收起候选后可一键重新选择', html.includes('aiCandidatesCollapsed = false'));
  check('手机端把原有浮动按钮抬高避让', html.includes('.bottom-buttons { bottom: 84px; }'));
  check('入库后有日志锚点可自动滚到结果', html.includes('id="aiCommitLogAnchor"'));
  check('搜索时显示已等待秒数', html.includes('已等待 <b>{{ aiSearchElapsed }}</b> 秒'));
  check('展示过滤掉的无关/失效链接数', html.includes('条失效链接'));

  // S04 这类季号目录要能讲清楚
  ctx.aiForm.season = 4;
  check('季号 4 → 目录名 S04', ctx.aiSeasonDir() === 'S04', ctx.aiSeasonDir());
  ctx.aiForm.season = 12;
  check('季号 12 → 目录名 S12', ctx.aiSeasonDir() === 'S12', ctx.aiSeasonDir());
  ctx.aiForm.season = '';
  check('不填季号则无子目录', ctx.aiSeasonDir() === '', ctx.aiSeasonDir());
  check('页面上解释了 S0X 的含义', html.includes('= 第 {{ aiForm.season }} 季'));
  check('文件夹行会说明「整包转存、不能单独改名」',
    html.includes('row.dir_hint') && html.includes('{{ row.dir_hint }}'));

  // 入库后重新分析时，滚动目标应是日志而不是列表顶部
  ctx.aiScrollTarget = 'log';
  const scrolled = [];
  ctx.aiScrollTo = function (id) { scrolled.push(id); };
  global.__nextResponse = {
    data: { success: true, data: { rows: [], stats: {}, rule: { valid: true }, dir_files: [] } }
  };
  ctx.aiForm.shareurl = 'https://pan.quark.cn/s/aaa';
  ctx.aiAnalyze();
  await tick();
  check('入库后滚到日志而不是列表顶部',
    scrolled[scrolled.length - 1] === 'aiCommitLogAnchor', scrolled);
  check('滚动目标用完即清空', ctx.aiScrollTarget === '');
}

(async () => {
  await testComponent();
  testHandlers();
  await testConfirmManualSelect();
  testSelectAll();
  await testAiFlow();
  await testFetchDataNormalize();
  await testPathResolution();
  testMobileMarkup();
  await testMobileInteraction();
  // 切换链接后结果要同步更新：失败时绝不能留着上一个链接的结果
  ctx.aiAnalysis = {
    rows: [{ status: 'new', file_name: 'old.mp4', new_name: 'x', selected: true }],
    stats: {}, rule: { valid: true }
  };
  global.__nextResponse = { data: { success: false, message: '分享地址已失效' } };
  ctx.aiForm.shareurl = 'https://pan.quark.cn/s/dead';
  ctx.aiAnalyze();
  await tick();
  check('分析失败时清空上一次的结果（不再显示旧链接的文件）',
    ctx.aiAnalysis === null, ctx.aiAnalysis);
  check('失败原因显示出来', /失效/.test(ctx.aiError || ''), ctx.aiError);
  check('分析请求序号递增（用于丢弃过期响应）', ctx.aiAnalyzeSeq > 0, ctx.aiAnalyzeSeq);

  // 删除已在库的文件
  ctx.aiAnalysis = {
    rows: [
      { status: 'exists', lib_name: 'E01.mp4', file_name: 'E01.mkv', selected: false },
      { status: 'new', lib_name: '', file_name: 'E02.mkv', selected: true }
    ],
    stats: {}, rule: { valid: true }
  };
  ctx.aiForm.savepath = '/0-影音/电视剧/X';
  check('统计出已在库的数量', ctx.aiExistsCount() === 1, ctx.aiExistsCount());
  calls.length = 0;
  global.__nextResponse = {
    data: { success: true, data: { ok: true, deleted: ['E01.mp4'], missing: [] } }
  };
  ctx.aiDeleteRow(ctx.aiAnalysis.rows[0]);
  await tick();
  const del = calls.find(c => c[1] === '/ai/delete');
  check('删除按钮调用 /ai/delete', !!del);
  check('按库里的文件名删除（不是分享里的名字）',
    !!del && del[2].file_names[0] === 'E01.mp4', del && del[2]);
  check('删除后自动刷新分析', calls.some(c => c[1] === '/ai/analyze'));

  // 切换链接要立刻清掉上一个链接的结果，避免等待期间看到旧数据
  ctx.aiResult = { candidates: [
    { taskname: 'A', shareurl: 'https://pan.quark.cn/s/a', score: 90 },
    { taskname: 'B', shareurl: 'https://pan.quark.cn/s/b', score: 80 },
  ] };
  ctx.aiAnalysis = { rows: [{ status: 'new', file_name: 'old.mp4' }], stats: {}, rule: {} };
  ctx.aiCommitLog = 'old log';
  global.__nextResponse = { data: { success: false, message: 'x' } };
  ctx.aiSelectIndex(1);
  check('切换链接后立刻清空上一个链接的分析结果', ctx.aiAnalysis === null, ctx.aiAnalysis);
  check('切换链接后清空上一个链接的入库日志', ctx.aiCommitLog === '');
  check('切换链接后筛选条件复位', ctx.aiFilter === 'all', ctx.aiFilter);
  await tick();

  // 搜索源没收录的分享：可以直接粘贴链接分析
  check('有「直接粘贴分享链接」入口', html.includes('aiAnalyzeDirect()'));
  check('手工链接入口绑定 shareurl', /v-model="aiForm.shareurl"/.test(html));
  const toastMsgs = [];
  ctx.showToast = function (m) { toastMsgs.push(m); };
  ctx.aiForm.keyword = '现在就出发';
  ctx.aiForm.taskname = '';
  ctx.aiForm.shareurl = 'https://example.com/not-quark';
  ctx.aiResult = { candidates: [] };
  ctx.aiAnalyzeDirect();
  check('非夸克链接会被拒绝', /不是夸克分享链接/.test(toastMsgs.join(' ')), toastMsgs);
  ctx.aiForm.shareurl = 'https://pan.quark.cn/s/7aa30043484a#/list/share';
  ctx.aiAnalyzeDirect();
  check('手工链接会用剧名补齐 taskname', ctx.aiForm.taskname === '现在就出发', ctx.aiForm.taskname);
  check('手工链接会清掉候选列表（没有搜索态）', ctx.aiResult === null, ctx.aiResult);
  await tick();

  // 手机端：候选列表必须是卡片，不能是表格（窄屏表格会溢出、操作列被切掉）
  check('候选列表用卡片而不是表格', html.includes('ai-cand') && !/table[\s\S]{0,200}aiResult\.candidates/.test(html));
  check('候选卡片有「分析这个分享」按钮', html.includes('分析这个分享'));
  check('候选卡片显示分享里最新的文件', html.includes('最新：{{ cand.latest_file }}'));
  check('候选列表给底部操作条留白', /class="ai-has-actionbar"/.test(html));

  // 排序：默认状态分组，可手动切成时间/名称
  check('有排序选择器', html.includes('v-model="aiSort"') && html.includes('时间 新→旧'));
  const sortRows = [
    { file_name: 'b.mp4', new_name: '现在就出发.E02.mkv', date_key: 20261010, updated_at: 1 },
    { file_name: 'a.mp4', new_name: '现在就出发.E03.mkv', date_key: 20261017, updated_at: 2 },
    { file_name: 'c.mp4', new_name: '现在就出发.E01.mkv', date_key: 20261001, updated_at: 3 },
  ];
  ctx.aiSort = 'time';
  check('按时间排序：新的在前',
    ctx.aiSortRows(sortRows).map(r => r.file_name).join(',') === 'a.mp4,b.mp4,c.mp4',
    ctx.aiSortRows(sortRows).map(r => r.file_name));
  ctx.aiSort = 'name';
  check('按重命名后的名称排序',
    ctx.aiSortRows(sortRows).map(r => r.file_name).join(',') === 'c.mp4,b.mp4,a.mp4',
    ctx.aiSortRows(sortRows).map(r => r.file_name));
  ctx.aiSort = 'source';
  check('按原文件名排序',
    ctx.aiSortRows(sortRows).map(r => r.file_name).join(',') === 'a.mp4,b.mp4,c.mp4');
  ctx.aiSort = 'status';
  check('状态分组保持原顺序',
    ctx.aiSortRows(sortRows).map(r => r.file_name).join(',') === 'b.mp4,a.mp4,c.mp4');

  // 预设规则跟着内容自动切换
  ctx.aiPatternTouched = false;
  ctx.aiForm.pattern = '$TV_MAGIC';
  ctx.aiApplyResolved({ category: '综艺', season: 4, savepath: '/x/S04',
                        suggested_pattern: '$ZONGYI_MAGIC', suggested_replace: 'Z' });
  check('搜到综艺时自动换成综艺规则', ctx.aiForm.pattern === '$ZONGYI_MAGIC', ctx.aiForm.pattern);
  ctx.aiPatternTouched = true;   // 用户手动改过
  ctx.aiApplyResolved({ category: '电视剧', season: 2, savepath: '/y/S02',
                        suggested_pattern: '$TV_MAGIC', suggested_replace: 'T' });
  check('用户手动选过预设就不再被覆盖', ctx.aiForm.pattern === '$ZONGYI_MAGIC', ctx.aiForm.pattern);
  ctx.aiPatternTouched = false;

  check('列表里有单条删除按钮', html.includes('aiDeleteRow(row)'));
  check('默认进入 AI 追剧页', html.includes("activeTab: 'ai'"));
  check('分析计时用 setTimeout 链（请求结束即停，不会泄漏）',
    /this\.aiAnalyzeTimer = setTimeout/.test(inlineJs), null);
  check('分析计时没有用 setInterval（泄漏会导致整页反复重渲染）',
    !/aiAnalyzeTimer = setInterval/.test(inlineJs), null);
  check('滚动进度条用瞬时滚动（smooth 会在元素移除后卡住）',
    /scrollIntoView\(\{ behavior: 'auto'/.test(inlineJs), null);
  check('候选链接可点开（新窗口）',
    html.includes('target="_blank"') && html.includes('cand.shareurl'));
  check('收起后的切换条也显示链接',
    html.includes('aiSelectedCandidate.shareurl'));
  check('展示文件大小', html.includes('row.size | size'));
  check('撞名时给出告警', html.includes('aiAnalysis.stats.collision'));
  check('展示花絮素材的去向 S00',
    html.includes('aiAnalysis.stats.extra') && html.includes('aiAnalysis.extra_dir'));
  check('每行标出花絮素材去向', html.includes('row.target_dir'));
  check('支持批量删除已在库', html.includes('aiDeleteAllExists'));

  // ---------------------------------------------------------------- 真实渲染
  // 用真实 Vue 编译并渲染整个模板：模板里任何非法表达式/未定义变量都会在这里暴露，
  // 而不是等到用户点开某个面板时页面卡死。
  console.log('\n【渲染】用真实 Vue 编译并渲染模板');
  try {
    // Vue 的完整版在加载时会读 navigator / document，先补齐最小桩
    if (!global.navigator || !global.navigator.userAgent) {
      Object.defineProperty(global, 'navigator', {
        value: { userAgent: 'node' }, configurable: true, writable: true,
      });
    }
    if (global.document && !global.document.createElement) {
      global.document.createElement = function () {
        return {
          innerHTML: '',
          children: [{ getAttribute: function () { return "'"; } }],
          setAttribute: function () {},
          getAttribute: function () { return "'"; },
          appendChild: function () {},
          style: {},
        };
      };
    }
    // 补齐 Vue 运行时可能用到的 DOM 接口
    const doc = global.document;
    if (doc) {
      doc.querySelector = doc.querySelector || function () { return null; };
      doc.querySelectorAll = doc.querySelectorAll || function () { return []; };
      doc.getElementById = doc.getElementById || function () { return null; };
      doc.addEventListener = doc.addEventListener || function () {};
      doc.removeEventListener = doc.removeEventListener || function () {};
      doc.body = doc.body || { appendChild: function () {}, removeChild: function () {}, classList: { add: function () {}, remove: function () {} } };
      doc.documentElement = doc.documentElement || { style: {}, classList: { add: function () {}, remove: function () {} } };
    }
    if (global.window && !global.window.navigator) {
      global.window.navigator = { userAgent: 'node' };
    }
    const VueReal = require(path.join(ROOT, 'app', 'static', 'js', 'vue@2.js'));
    const appStart = html.indexOf('<div id="app">');
    const scriptStart = html.indexOf('<script', appStart);
    const template = html.slice(appStart, scriptStart);
    check('模板根元素可定位', appStart > 0 && scriptStart > appStart);

    const compiled = VueReal.compile(template);
    check('模板编译无错误', typeof compiled.render === 'function');

    const vueOptions = Object.assign({}, options, {
      render: compiled.render,
      staticRenderFns: compiled.staticRenderFns,
      data: () => JSON.parse(JSON.stringify(options.data)),
    });
    delete vueOptions.el;   // 不挂载，只渲染，避免依赖真实 DOM
    const vm = new VueReal(vueOptions);
    check('组件实例创建成功', !!vm);

    // 造一份真实形态的分析结果，覆盖分析结果区、花絮提示、删除按钮等
    vm.aiAnalysis = {
      rows: [{
        fid: 'f1', file_name: '20261012-第1期加更.mp4',
        new_name: '现在就出发.S00E138.第1期加更.mp4',
        status: 'new', status_text: '可新增', selected: true,
        size: 474877906, updated_at: 1792213701882, dir: false,
        dir_path: 'X现在JCF44/', parent_fid: 'p1', in_rule: true,
        is_extra: true, target_dir: 'S00', target_savepath: '/0-影音/综艺/现在就出发/S00',
        lib_name: '', tmdb_episode: 138, tmdb_match: '日期 2026-10-12',
        default_selected: true,
      }],
      stats: { total: 1, new: 1, exists: 0, dup_episode: 0, unmatched: 0,
               folder: 0, collision: 0, extra: 1 },
      savepath: '/0-影音/综艺/现在就出发/S04', savepath_exists: true,
      dir_note: '', dir_file_count: 2,
      extra_dir: '/0-影音/综艺/现在就出发/S00', extra_exists: false,
      extra_note: '', tmdb_matched: 1, tmdb_enabled: true,
      rule: { valid: true, pattern: '', replace: '', resolved_pattern: '',
              resolved_replace: '{TASKNAME}.{SXX}E{E}.{EXT}', missing: [], unresolved: [] },
      rule_suggestion: null, truncated: false, expanded_count: 1,
    };
    vm.aiResult = {
      candidates: [{ taskname: '现在就出发 第四季', shareurl: 'https://pan.quark.cn/s/abc',
                     score: 98, source: 'pansou', datetime: '', channel: '' }],
      intent: { title: '现在就出发', season: null, category: '综艺', aliases: [], queries: ['现在就出发'] },
      filtered: { irrelevant: 0, dead: 0, low_score: 0 },
      pool_size: 1, from_cache: false, retried: false, attempts: 1, hint: '',
    };
    vm.aiSelectedCandidate = vm.aiResult.candidates[0];
    vm.aiCandidatesCollapsed = true;
    vm.aiAnalyzing = false;

    let renderErr = null;
    try {
      vm._render();
    } catch (e) {
      renderErr = e;
    }
    check('分析结果区渲染无异常', !renderErr, renderErr && String(renderErr.message || renderErr));

    // 分析进行中的状态也要能渲染
    vm.aiAnalyzing = true;
    vm.aiAnalyzeElapsed = 3;
    let renderErr2 = null;
    try {
      vm._render();
    } catch (e) {
      renderErr2 = e;
    }
    check('分析中（进度条）渲染无异常', !renderErr2, renderErr2 && String(renderErr2.message || renderErr2));
  } catch (e) {
    check('真实渲染测试可运行', false, String(e && e.message || e));
  }

  console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
  process.exit(failed ? 1 : 0);
})();
