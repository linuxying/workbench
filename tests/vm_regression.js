/**
 * 工作台全模块 VM 回归测试（重复任务版）
 * 用法：node tests/vm_regression.js [http://127.0.0.1:5000]
 * 原理：抽取 served 版页面 <script>，在 Node VM + mock DOM 中真实执行，注入数据检查渲染。
 * mock 要点（缺一会产生假失败掩盖真 bug）：
 *   1. textContent 赋值 → innerHTML 转义序列化（escapeHtml 依赖）
 *   2. querySelectorAll 返回 []；createElement 产物要记录（表格写进新建 tbody）
 *   3. 页面顶层 let 不在 global 上——注入用第二个脚本 vm.runInContext
 *   4. switchBoardMode 会调 loadTasks()——mock fetch 用永不 resolve 的 Promise，
 *      并直接调 renderTaskTable 绕过
 */
const vm = require('vm');

const BASE = process.argv[2] || 'http://127.0.0.1:5000';

async function main() {
  const res = await fetch(BASE + '/');
  const html = await res.text();
  const m = html.match(/<script>([\s\S]*?)<\/script>/g);
  let src = '';
  for (const s of m) {
    const b = s.replace(/^<script>/, '').replace(/<\/script>$/, '');
    if (b.length > src.length) src = b;
  }
  console.log('served app bytes:', src.length);

  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const created = [];
  function makeEl(id) {
    const el = {
      id, _html: '', style: {}, dataset: {}, children: [],
      get innerHTML() { return el._html; }, set innerHTML(v) { el._html = v; },
      get textContent() { return el._text || ''; },
      set textContent(v) { el._text = (v == null ? '' : String(v)); el._html = esc(el._text); },
      classList: {
        _s: new Set(),
        add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
        toggle(c, f) { f === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c)) : (f ? this._s.add(c) : this._s.delete(c)); },
        contains(c) { return this._s.has(c); }
      },
      value: '', checked: false,
      addEventListener() {}, appendChild(c) { el.children.push(c); return c; },
      querySelector() { return null; }, querySelectorAll() { return []; },
      closest() { return null; }, contains() { return false; }, remove() {},
      scrollIntoView() {}, scrollTo() {}, focus() {}, blur() {}
    };
    return el;
  }
  const els = {}; let domReady = null;
  const document = {
    getElementById(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; },
    querySelector(s) { if (s === '.task-board') { if (!els['__b']) els['__b'] = makeEl('__b'); return els['__b']; } return null; },
    querySelectorAll() { return []; },
    createElement(tag) { const e = makeEl('dyn-' + tag); created.push(e); return e; },
    addEventListener(t, f) { if (t === 'DOMContentLoaded') domReady = f; },
    body: { innerHTML: '' }
  };
  const store = {};
  const sandbox = {
    document,
    localStorage: { getItem(k) { return k in store ? store[k] : null; }, setItem(k, v) { store[k] = String(v); }, removeItem(k) { delete store[k]; } },
    fetch: () => new Promise(() => {}),
    confirm: () => true, alert: () => {}, console,
    setTimeout, setInterval: () => 0, clearInterval: () => {}, clearTimeout: () => {}, Promise
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);

  const results = [];
  const check = (n, c) => { results.push([c ? 'PASS' : 'FAIL', n]); if (!c) process.exitCode = 1; };

  ['remindPanel', 'rcStats', 'rcSections', 'rcHint', 'bellBadge',
   'todoTasks', 'inProgressTasks', 'doneTasks',
   'todoCount', 'inProgressCount', 'doneCount', 'boardStat'].forEach(id => els[id] = makeEl(id));
  els['remindPanel'].style.display = 'none';

  domReady();
  await new Promise(r => setTimeout(r, 300));

  // ---- A. 看板 ----
  vm.runInContext(`
    currentTasks = [
      {id:1,title:'逾期任务甲',description:'',status:'todo',priority:'high',project:'客户甲',due_date:'2026-09-01',link_url:'',subtask_total:0,subtask_done:0,progress_count:0,latest_progress:'',repeat_freq:'daily',repeat_day:null},
      {id:2,title:'进行任务乙',description:'',status:'in_progress',priority:'medium',project:'客户乙',due_date:'2026-09-10',link_url:'',subtask_total:0,subtask_done:0,progress_count:0,latest_progress:'',repeat_freq:'none',repeat_day:null},
      {id:3,title:'完成任务丙',description:'',status:'done',priority:'low',project:'',due_date:'',link_url:'',subtask_total:0,subtask_done:0,progress_count:0,latest_progress:'',repeat_freq:'none',repeat_day:null}
    ];
    renderBoard();
  `, sandbox);
  const boardAll = ['todoTasks', 'inProgressTasks', 'doneTasks'].map(i => els[i].innerHTML).join('');
  check('A1 看板卡片渲染', boardAll.includes('task-card'));
  check('A2 卡片标题非空', /task-title">[^<]+</.test(boardAll));
  check('A3 重复任务徽标', boardAll.includes('🔁 每日'));

  vm.runInContext('toggleBoardGroupBy();', sandbox);
  check('B1 看板分组组头渲染', (els['todoTasks'].innerHTML + els['inProgressTasks'].innerHTML).includes('board-group-header'));
  vm.runInContext('toggleBoardGroupBy();', sandbox);

  // ---- C. 表格（直接调 renderTaskTable 绕过 loadTasks fetch）----
  const before = created.length;
  vm.runInContext(`
    currentTasks = currentTasks.filter(t => t.status !== 'done');
    renderTaskTable();
  `, sandbox);
  const tableHtml = created.slice(before).map(e => e._html).join('');
  check('C1 表格行渲染', tableHtml.includes('tf-row-check') || tableHtml.includes('<tr'));
  check('C2 表格标题非空', tableHtml.includes('逾期任务甲') && tableHtml.includes('进行任务乙'));
  check('C3 表格重复标记', tableHtml.includes('🔁 每日'));

  // ---- D. 提醒面板（里程碑三类 + 未设截止日池）----
  vm.runInContext(`
    remindData = {today:'2026-09-04',
      overdue:{count:1,items:[{id:99,title:'逾期任务X',project:'P1',priority:'high',due_date:'2026-09-01'}]},
      due_today:{count:2,items:[{id:97,title:'今日到期Y',project:'P2',priority:'medium',due_date:'2026-09-04'}]},
      due_soon:{count:1,items:[{id:96,title:'临期Z',project:'P3',priority:'low',due_date:'2026-09-06'}]},
      no_due:{count:2,items:[{id:95,title:'无截止W',project:'P4',priority:'medium'},{id:94,title:'无截止V',project:'P5',priority:'medium'}]}};
    renderReminders();
  `, sandbox);
  check('D1 角标计数=4(不含未设截止日)', String(els['bellBadge'].textContent) === '4');
  const ph = els['remindPanel'].innerHTML;
  check('D2 三个分组齐全且无未汇报', ['已逾期', '今日到期', '未来 7 天到期'].every(s => ph.includes(s)) && !ph.includes('未汇报'));
  check('D3 无例行残留分组', !ph.includes('例行未打卡'));

  // ---- E. 提醒开关循环 ----
  vm.runInContext('toggleReminders();', sandbox);
  check('E1 打开', els['remindPanel'].style.display === '');
  vm.runInContext('toggleReminders();', sandbox);
  check('E2 关闭', els['remindPanel'].style.display === 'none');

  // ---- F. 里程碑页面（时间轴视图）----
  vm.runInContext(`
    remindData = {
      today: '2026-09-11',
      counts: {overdue:1, today:1, week:1, month:1, later:1, unscheduled:1, window:3},
      sections: [
        {key:'now', label:'需要立刻处理', hint:'逾期与今日到期', items:[
          {id:99,title:'逾期承诺甲',project:'客户甲',priority:'high',kind:'commit',kind_label:'承诺',due_date:'2026-09-01',days_left:-10,days_late:10,in_window:true,remind_advance:3},
          {id:98,title:'今日节律乙',project:'客户乙',priority:'medium',kind:'routine',kind_label:'节律',deliverable:'巡检报告',repeat_desc:'每月 11 日',due_date:'2026-09-11',days_left:0,days_late:0,in_window:true,remind_advance:30}
        ]},
        {key:'soon', label:'未来 7 天', hint:'临近，提前安排', items:[
          {id:97,title:'研发计划项目丙',project:'客户丙',priority:'medium',kind:'plan',kind_label:'计划',due_date:'2026-09-15',days_left:4,days_late:0,in_window:true,remind_advance:1,stage_desc:'1/3 阶段完成',next_node:'联调完成'}
        ]},
        {key:'month', label:'未来 30 天', hint:'已进入提醒窗口', items:[
          {id:96,title:'月中承诺丁',project:'客户丁',priority:'low',kind:'commit',kind_label:'承诺',due_date:'2026-10-05',days_left:24,days_late:0,in_window:true,remind_advance:3}
        ]},
        {key:'later', label:'更远', hint:'含年度合同事项', items:[
          {id:95,title:'合同年度巡检戊',project:'客户戊',priority:'low',kind:'routine',kind_label:'节律',deliverable:'年度巡检报告',repeat_desc:'每年 6 月 20 日',due_date:'2027-06-20',days_left:282,days_late:0,in_window:false,remind_advance:30}
        ]},
        {key:'unscheduled', label:'待排期', hint:'未设日期，点开补设', items:[
          {id:94,title:'无日期事项己',project:'客户己',priority:'medium',kind:'task',kind_label:'任务',due_date:'',days_left:null,days_late:0,in_window:false,remind_advance:0}
        ]}
      ],
      overdue:{count:1,items:[{id:99,title:'逾期承诺甲'}]},
      due_today:{count:1,items:[{id:98,title:'今日节律乙'}]},
      due_soon:{count:1,items:[{id:97,title:'研发计划项目丙'}]},
      no_due:{count:1,items:[{id:94,title:'无日期事项己'}]}
    };
    renderReminderPage(remindData);
  `, sandbox);
  check('F1 四张统计卡', (els['rcStats'].innerHTML.match(/rc-card/g) || []).length === 4);
  const rsec = els['rcSections'].innerHTML;
  check('F2 条目可跳转任务弹窗', rsec.includes('rcOpenTask('));
  check('F3 时间轴五段分组齐全',
    ['需要立刻处理', '未来 7 天', '未来 30 天', '更远', '待排期'].every(s => rsec.includes(s)));
  check('F4 三类语义徽章齐全', ['节律', '承诺', '计划'].every(s => rsec.includes(s)));
  check('F5 节律显示交付物与周期描述', rsec.includes('巡检报告') && rsec.includes('每月 11 日'));
  check('F6 计划显示阶段进度与下一节点', rsec.includes('1/3 阶段完成') && rsec.includes('联调完成'));
  check('F7 承诺逾期标红', rsec.includes('rc-over') && rsec.includes('rc-late'));
  check('F8 年度事项描述正确', rsec.includes('每年 6 月 20 日'));
  check('F9 无日期项显示未排期', rsec.includes('未排期'));
  check('F10 类型色条类名', rsec.includes('rc-item-routine') && rsec.includes('rc-item-commit') && rsec.includes('rc-item-plan'));

  // ---- H. 关键函数与重复 UI ----
  check('H1 关键函数齐全', ['backupNow', 'exportTableCSV', 'toast', 'taskRepeatChange', 'repeatLabel',
    'showAddTaskModal', 'showEditTaskModal', 'saveTask', 'pickKind', 'loadSubtasks', 'addSubtask']
    .every(f => typeof sandbox[f] === 'function'));
  check('H2 弹窗重复字段存在', !!document.getElementById('taskRepeatFreq') &&
    !!document.getElementById('taskRemindAdvance') && !!document.getElementById('taskRepeatDay'));
  vm.runInContext(`
    document.getElementById('taskRepeatFreq').value = 'monthly';
    taskRepeatChange();
  `, sandbox);
  check('H3 每月→日期下拉31项(含月底)', els['taskRepeatDay'].innerHTML.includes('月底'));

  // ---- I. 事项类型（时间语义）联动 ----
  check('I1 类型选择器与节点区节点存在', !!document.getElementById('taskKind') &&
    !!document.getElementById('taskKindPicker') && !!document.getElementById('subtaskList'));
  vm.runInContext(`showAddTaskModal('todo');`, sandbox);
  check('I2 新建默认普通任务', els['taskKind'].value === 'task');
  check('I3 普通任务隐藏交付物字段', els['taskDeliverableBox'].style.display === 'none');
  vm.runInContext(`pickKind('routine');`, sandbox);
  check('I4 节律自动切换为月度周期', els['taskRepeatFreq'].value === 'monthly');
  check('I5 节律默认提前 30 天', String(els['taskRemindAdvance'].value) === '30');
  check('I6 节律显示交付物字段', els['taskDeliverableBox'].style.display === '');
  check('I7 节律日期语义=本期发生日', els['taskDueLabel'].textContent.includes('发生日'));
  vm.runInContext(`pickKind('plan');`, sandbox);
  check('I8 计划显示节点管理区', els['subtaskBox'].style.display === '');
  check('I9 计划默认提前 1 天', String(els['taskRemindAdvance'].value) === '1');
  vm.runInContext(`pickKind('commit');`, sandbox);
  check('I10 承诺默认提前 3 天', String(els['taskRemindAdvance'].value) === '3');
  vm.runInContext(`document.getElementById('taskRepeatFreq').value='yearly'; taskRepeatChange();`, sandbox);
  check('I11 年频显示月份选择', els['taskRepeatMonthBox'].style.display === '');
  check('I12 年频月份 12 项', (els['taskRepeatMonth'].innerHTML.match(/<option/g) || []).length === 12);

  // ---- J. 计划节点（子任务）函数 ----
  check('J1 子任务函数齐全', ['loadSubtasks', 'addSubtask', 'toggleSubtask', 'deleteSubtask']
    .every(f => typeof sandbox[f] === 'function'));

  // ---- K. 卡片紧迫度信号 / 描述浮层 / 密度档位 ----
  vm.runInContext(`
    currentDate = '2026-09-20';
    tableFilter = { status: '', project: '', overdue: false };
    const _t = (id, title, status, due) => ({ id, title, description: '描述' + id, status, priority: 'high',
      project: '', due_date: due, link_url: '', subtask_total: 0, subtask_done: 0,
      progress_count: 0, latest_progress: '', repeat_freq: 'none', repeat_day: null });
    currentTasks = [
      _t(11, '逾期甲', 'todo', '2026-09-18'),
      _t(12, '今日乙', 'todo', '2026-09-20'),
      _t(13, '临期丙', 'todo', '2026-09-22'),
      _t(14, '远期丁', 'todo', '2026-10-20'),
      _t(15, '已完成戊', 'done', '2026-09-01')
    ];
    renderBoard();
  `, sandbox);
  const kTodo = els['todoTasks'].innerHTML;
  const kDone = els['doneTasks'].innerHTML;
  const cardCls = id => {
    const m = kTodo.match(new RegExp('class="task-card([^"]*)"\\s+data-id="' + id + '"'));
    return m ? m[1] : null;
  };
  check('K1 逾期卡片带 is-overdue', /is-overdue/.test(cardCls(11) || ''));
  check('K2 逾期文案含天数', kTodo.includes('逾期 2 天'));
  check('K3 今日到期带 is-today', /is-today/.test(cardCls(12) || '') && kTodo.includes('今天到期'));
  check('K4 临期带 is-soon', /is-soon/.test(cardCls(13) || '') && kTodo.includes('2 天后'));
  check('K5 远期卡片无紧迫度类', (() => {
    const c = cardCls(14) || '';
    return !/is-overdue|is-today|is-soon/.test(c);
  })());
  check('K6 已完成不参与紧迫度', !/is-overdue|is-today|is-soon/.test(kDone));
  check('K7 描述包裹层存在', kTodo.includes('task-desc-wrap'));
  check('K8 日期标签带 tag-due 类', kTodo.includes('tag-due'));
  check('K9 距今天数函数正确', vm.runInContext(
    "daysFromToday('2026-09-20') === 0 && daysFromToday('2026-09-18') === -2 && daysFromToday('2026-09-25') === 5", sandbox));
  vm.runInContext(`applyDensity('compact');`, sandbox);
  check('K10 紧凑档切换生效', els['taskBoard'].classList.contains('is-compact'));
  check('K11 紧凑档写入偏好', store['wb_board_density'] === 'compact');
  check('K12 紧凑按钮态正确', els['denCompact'].classList.contains('active') && !els['denStd'].classList.contains('active'));
  vm.runInContext(`switchDensity('standard');`, sandbox);
  check('K13 标准档取消紧凑', !els['taskBoard'].classList.contains('is-compact') && store['wb_board_density'] === 'standard');
  check('K14 密度函数齐全', typeof sandbox.switchDensity === 'function' && typeof sandbox.applyDensity === 'function');
  vm.runInContext(`applyBoardMode('table');`, sandbox);
  check('K15 表格模式隐藏密度档', els['densityToggle'].style.display === 'none');
  vm.runInContext(`applyBoardMode('card');`, sandbox);
  check('K16 卡片模式恢复密度档', els['densityToggle'].style.display === '');

  // ---- L. 描述浮层：卡片外展开 / 方向自适应（修复「浮层遮挡卡内链接」） ----
  check('L1 浮层方向函数存在', typeof sandbox.orientDescPanel === 'function');
  check('L2 触发范围收在描述行', /\.task-desc-wrap:hover \.task-desc\{/.test(html));
  check('L3 浮层落在卡片外侧(top:100%)', /\.task-desc-wrap:hover \.task-desc\{[^}]*top:100%/.test(html));
  check('L4 浮层不拦截指针', /\.task-desc-wrap:hover \.task-desc\{[^}]*pointer-events:none/.test(html));
  check('L5 旧的卡内覆盖规则已移除', !/\.task-card:hover \.task-desc\{/.test(html));
  check('L6 悬停卡片抬升层级', /\.task-card:hover\{[^}]*z-index:60/.test(html));
  check('L7 向上展开分支存在', /\.task-desc-wrap\.desc-panel-up:hover \.task-desc\{/.test(html));

  // 方向自适应逻辑（纯函数级，注入伪几何）
  const fakeWrap = (above, below) => {
    const panel = { style: {} };
    const w = {
      _up: null,
      classList: { toggle(c, f) { if (c === 'desc-panel-up') w._up = !!f; } },
      querySelector() { return panel; },
      closest(sel) {
        if (sel === '.task-card') return { getBoundingClientRect: () => ({ top: 500, bottom: 600 }) };
        if (sel === '.task-column-body') return { getBoundingClientRect: () => ({ top: 500 - above, bottom: 600 + below }) };
        return null;
      }
    };
    return { w, panel };
  };
  const t1 = fakeWrap(400, 300);
  sandbox.orientDescPanel(t1.w);
  check('L8 下方充裕→向下展开且限高200', t1.w._up === false && t1.panel.style.maxHeight === '200px');
  const t2 = fakeWrap(400, 50);
  sandbox.orientDescPanel(t2.w);
  check('L9 下方不足→向上展开', t2.w._up === true && t2.panel.style.maxHeight === '200px');
  const t3 = fakeWrap(40, 50);
  sandbox.orientDescPanel(t3.w);
  check('L10 上下皆窄→保持向下并限高96', t3.w._up === false && t3.panel.style.maxHeight === '96px');
  let lOk = true;
  try {
    sandbox.orientDescPanel(null);
    sandbox.orientDescPanel({});
    sandbox.orientDescPanel({ closest: () => null });
  } catch (e) { lOk = false; }
  check('L11 异常入参不抛错', lOk);

  // ---- M. 看板排序与优先级视觉 ----
  console.log('---------------- M. 看板排序 ----------------');
  vm.runInContext(`
    var __dOff = function (n) {
      var d = new Date(Date.parse(currentDate + 'T00:00:00') + n * 86400000);
      var mm = String(d.getMonth() + 1); if (mm.length < 2) mm = '0' + mm;
      var dd = String(d.getDate()); if (dd.length < 2) dd = '0' + dd;
      return d.getFullYear() + '-' + mm + '-' + dd;
    };
    var __mk = function (id, status, priority, dueOff) {
      return {
        id: id, title: 'T' + id, description: '', status: status, priority: priority,
        project: '', due_date: (dueOff === null ? '' : __dOff(dueOff)),
        link_url: '', subtask_total: 0, subtask_done: 0, progress_count: 0,
        latest_progress: '', repeat_freq: 'none', repeat_day: null, kind: 'task',
        completed_date: '', updated_at: '2026-09-01 00:00:0' + (id % 10)
      };
    };
  `, sandbox);

  const tierRes = vm.runInContext(`[
    boardTier(__mk(1,'todo','medium',-3)),
    boardTier(__mk(2,'todo','medium',0)),
    boardTier(__mk(3,'todo','high',10)),
    boardTier(__mk(4,'todo','medium',2)),
    boardTier(__mk(5,'todo','medium',30)),
    boardTier(__mk(6,'todo','medium',null))
  ]`, sandbox);
  check('M1 紧迫度分层：逾期/今日/高优先级/三天内/远期/无期',
    JSON.stringify(tierRes) === JSON.stringify([0, 1, 2, 3, 4, 4]));

  const urgOrder = vm.runInContext(`
    boardSortMode = 'urgency';
    boardSortTasks([ __mk(1,'todo','medium',null), __mk(2,'todo','medium',30),
                     __mk(3,'todo','high',null),   __mk(4,'todo','medium',2),
                     __mk(5,'todo','medium',-1),   __mk(6,'todo','medium',0) ])
      .map(function (t) { return t.id; })`, sandbox);
  check('M2 紧急优先：逾期→今日→高优先级→三天内→其余',
    JSON.stringify(urgOrder) === JSON.stringify([5, 6, 3, 4, 2, 1]));

  const priOrder = vm.runInContext(`
    boardSortMode = 'priority';
    boardSortTasks([ __mk(1,'todo','medium',null), __mk(2,'todo','low',null),
                     __mk(3,'todo','high',null),   __mk(4,'todo','medium',5) ])
      .map(function (t) { return t.id; })`, sandbox);
  check('M3 优先级排序：高→中→低，同档按截止日升序',
    JSON.stringify(priOrder) === JSON.stringify([3, 4, 1, 2]));

  const dueOrder = vm.runInContext(`
    boardSortMode = 'due';
    boardSortTasks([ __mk(1,'todo','medium',null), __mk(2,'todo','medium',30),
                     __mk(3,'todo','medium',2),    __mk(4,'todo','medium',-5) ])
      .map(function (t) { return t.id; })`, sandbox);
  check('M4 截止时间排序：日期升序，无截止日沉底',
    JSON.stringify(dueOrder) === JSON.stringify([4, 3, 2, 1]));

  const updOrder = vm.runInContext(`
    var a = __mk(1,'todo','medium',null); a.updated_at = '2026-09-01 10:00:00';
    var b = __mk(2,'todo','medium',null); b.updated_at = '2026-09-09 10:00:00';
    var c = __mk(3,'todo','medium',null); c.updated_at = '2026-09-05 10:00:00';
    boardSortMode = 'updated';
    boardSortTasks([a, b, c]).map(function (t) { return t.id; })`, sandbox);
  check('M5 最近更新排序：更新时间倒序',
    JSON.stringify(updOrder) === JSON.stringify([2, 3, 1]));

  const doneOrder = vm.runInContext(`
    var a = __mk(1,'done','medium',null); a.completed_date = '2026-09-01';
    var b = __mk(2,'done','medium',null); b.completed_date = '2026-09-08';
    var c = __mk(3,'done','medium',null); c.completed_date = '';
    boardSortMode = 'urgency';
    boardSortTasks([a, b, c]).map(function (t) { return t.id; })`, sandbox);
  check('M6 已完成列按完成日期倒序，不受排序切换影响',
    JSON.stringify(doneOrder) === JSON.stringify([2, 1, 3]));

  const weirdOrder = vm.runInContext(`
    boardSortMode = 'priority';
    boardSortTasks([ __mk(1,'todo','',null), __mk(2,'todo','URGENT',null) ])
      .map(function (t) { return t.id; })`, sandbox);
  check('M7 非法/空优先级归入默认档且不抛错',
    JSON.stringify(weirdOrder) === JSON.stringify([2, 1]));

  const emptyRes = vm.runInContext(`[ boardSortTasks([]).length, boardSortTasks(null).length ]`, sandbox);
  check('M8 空数组与 null 入参安全', JSON.stringify(emptyRes) === JSON.stringify([0, 0]));

  check('M9 排序控件四档齐全',
    ['bsmUrgency', 'bsmPriority', 'bsmDue', 'bsmUpdated'].every(id => html.indexOf('id="' + id + '"') >= 0));

  const fallbackRes = vm.runInContext(`
    boardSortMode = 'urgency';
    applyBoardSort('bogus', true);
    boardSortMode`, sandbox);
  check('M10 非法排序模式回退紧急优先', fallbackRes === 'urgency');
  check('M11 排序偏好写入 localStorage', store['wb_board_sort'] === 'urgency');

  const prioCls = vm.runInContext(`(function () {
    var d = __mk(9,'done','high',null);
    return {
      high: taskCardHtml(__mk(1,'todo','high',null)).indexOf('task-card prio-high') >= 0,
      medium: taskCardHtml(__mk(2,'todo','medium',null)).indexOf('task-card prio-medium') >= 0,
      low: taskCardHtml(__mk(3,'todo','low',null)).indexOf('task-card prio-low') >= 0,
      weird: taskCardHtml(__mk(4,'todo','X',null)).indexOf('task-card prio-medium') >= 0,
      done: taskCardHtml(d).indexOf('prio-high done') >= 0
    };
  })()`, sandbox);
  check('M12 卡片带优先级视觉 class（高/中/低，非法归中）',
    prioCls.high && prioCls.medium && prioCls.low && prioCls.weird);
  check('M13 已完成卡片 class 组合为 prio-high done', prioCls.done);

  check('M14 高优先级顶边条 CSS 契约',
    /\.task-card\.prio-high:not\(\.done\)::before/.test(html) && html.indexOf('var(--warn-500)') >= 0);
  check('M15 中/低档不带顶边条',
    !/\.task-card\.prio-medium[^{]*::before/.test(html) && !/\.task-card\.prio-low[^{]*::before/.test(html));
  check('M16 高优先级标签改用琥珀，不再占用品牌蓝',
    html.indexOf('.tag-priority-high{background:var(--warn-500)') >= 0);
  check('M17 已完成的高优先级卡片不显示顶边条（done 排除生效）',
    html.indexOf('.task-card.prio-high:not(.done)::before') >= 0);

  const tblOrder = vm.runInContext(`
    tableSort = { key: 'priority', dir: 'asc' };
    sortTasks([ __mk(1,'todo','low',null), __mk(2,'todo','high',null) ])
      .map(function (t) { return t.id; })`, sandbox);
  check('M18 表格模式列头排序仍独立工作',
    JSON.stringify(tblOrder) === JSON.stringify([2, 1]));

  // ---- N. 看板检索（模糊匹配 / 高亮转义 / 双入口同步 / 与筛选叠加）----
  // 这一组的价值在于把"搜不到"的每一种可能都钉住：字段覆盖面、AND 语义、
  // 高亮转义安全、检索期间强制展开分组、清空后回落全量。
  console.log('---------------- N. 看板检索 ----------------');
  vm.runInContext(`
    var __mkS = function (id, title, opt) {
      opt = opt || {};
      var t = __mk(id, opt.status || 'todo', opt.priority || 'medium', null);
      t.title = title;
      t.description = opt.desc || '';
      t.project = opt.project || '';
      t.category = opt.category || '';
      t.link_url = opt.link || '';
      t.completion_note = opt.note || '';
      t.latest_progress = opt.progress || '';
      return t;
    };
    var __SETS = [
      __mkS(1, '甲银行漏洞修复', { project: '甲银行', desc: '移动应用安全监测平台' }),
      __mkS(2, '乙行现场支持', { project: '乙银行', desc: '加固问题排查' }),
      __mkS(3, '年终总结', { category: '内部事务', note: '已提交述职报告', status: 'done' }),
      __mkS(4, 'Harmony 策略转换', { project: '丙银行', progress: 'v2.7 已完成转换' }),
      __mkS(5, 'iOS 加固', { link: 'https://example.com/task/123', desc: 'IPA 策略' })
    ];
  `, sandbox);

  check('N1 检索函数族齐全',
    ['boardSearchKeywords', 'boardSearchHaystack', 'boardMatchKeywords', 'filterByBoardKeyword',
     'highlightKeywords', 'onBoardSearchInput', 'onHeaderSearchInput', 'clearBoardSearch',
     'syncBoardSearch', 'focusBoardSearch', 'onBoardSearchKeydown']
      .every(f => typeof sandbox[f] === 'function'));

  const nHit = (kw) => vm.runInContext(
    `boardKeyword = ${JSON.stringify(kw)}; filterByBoardKeyword(__SETS).map(function (t) { return t.id; })`, sandbox);
  const jEq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  check('N2 空词与纯空格不过滤，返回全量', jEq(nHit(''), [1, 2, 3, 4, 5]) && jEq(nHit('   '), [1, 2, 3, 4, 5]));
  check('N3 标题命中', jEq(nHit('漏洞'), [1]));
  check('N4 描述命中', jEq(nHit('排查'), [2]));
  check('N5 项目/客户名命中（旧实现搜不到的字段）',
    jEq(nHit('乙银行'), [2]) && jEq(nHit('丙'), [4]));
  check('N6 分类 / 完成说明 / 最新进展 / 链接均参与匹配',
    jEq(nHit('内部事务'), [3]) && jEq(nHit('述职'), [3]) &&
    jEq(nHit('v2.7'), [4]) && jEq(nHit('example.com'), [5]));
  check('N7 空格分词为 AND 语义（宁可少，不可噪音）',
    jEq(nHit('甲银行 漏洞'), [1]) && jEq(nHit('甲银行 乙行'), []));
  check('N8 大小写不敏感', jEq(nHit('HARMONY'), [4]) && jEq(nHit('harmony'), [4]));
  check('N9 无命中返回空数组，不抛错', jEq(nHit('zzz不存在zzz'), []));

  const hl = vm.runInContext(`(function () {
    return {
      hit: highlightKeywords('甲银行漏洞修复', ['漏洞']),
      miss: highlightKeywords('乙行现场支持', ['漏洞']),
      multi: highlightKeywords('漏洞与漏洞', ['漏洞']).split('<mark class="kw-hit">').length - 1,
      esc: highlightKeywords('<img src=x onerror=alert(1)>', ['img']),
      escMiss: highlightKeywords('<script>alert(1)</script>', ['zzz']),
      escInHit: highlightKeywords('a<b&c', ['<b&']),
      overlap: highlightKeywords('abcabc', ['abc', 'bca']).split('<mark class="kw-hit">').length - 1,
      plain: highlightKeywords('纯文本', [])
    };
  })()`, sandbox);
  check('N10 命中片段包 mark.kw-hit', hl.hit === '甲银行<mark class="kw-hit">漏洞</mark>修复');
  check('N11 未命中不产生 mark，空词走纯转义', hl.miss.indexOf('<mark') === -1 && hl.plain === '纯文本');
  check('N12 同一词多次命中各自高亮', hl.multi === 2);
  check('N13 高亮路径先转义后拼接，不产生注入',
    hl.esc.indexOf('<img') === -1 && hl.esc.indexOf('&lt;') >= 0 && hl.esc.indexOf('&gt;') >= 0 &&
    hl.escMiss === '&lt;script&gt;alert(1)&lt;/script&gt;' &&
    hl.escInHit === 'a<mark class="kw-hit">&lt;b&amp;</mark>c');
  check('N14 重叠命中区间被合并，不产生嵌套 mark', hl.overlap === 1);

  const syncRes = vm.runInContext(`(function () {
    currentTasks = __SETS;
    boardKeyword = '甲银行';
    syncBoardSearch('all');
    var hit = document.getElementById('boardSearchHit');
    var tb = document.getElementById('boardToolbar');
    var res = {
      hitHtml: hit.innerHTML, searching: tb.classList.contains('is-searching'),
      input: document.getElementById('boardSearchInput').value,
      header: document.getElementById('searchInput').value
    };
    boardKeyword = 'zzz不存在';
    syncBoardSearch('all');
    res.emptyHit = hit.innerHTML;
    res.isEmpty = hit.classList.contains('is-empty');
    boardKeyword = '';
    syncBoardSearch('all');
    res.cleared = !tb.classList.contains('is-searching') && hit.textContent === '';
    return res;
  })()`, sandbox);
  check('N15 命中回显「命中 N / M」', syncRes.hitHtml.indexOf('命中 <b>1</b> / 5') >= 0);
  check('N16 检索态类 is-searching 正确开关', syncRes.searching === true && syncRes.cleared === true);
  check('N17 看板入口与顶栏入口双向同步', syncRes.input === '甲银行' && syncRes.header === '甲银行');
  check('N18 零命中回显转灰（is-empty）',
    syncRes.isEmpty === true && syncRes.emptyHit.indexOf('命中 <b>0</b> / 5') >= 0);

  const renderRes = vm.runInContext(`(function () {
    currentTasks = __SETS;
    boardGroupByProject = false;
    onBoardSearchInput('甲银行');
    var todo = document.getElementById('todoTasks').innerHTML;
    return {
      cards: (todo.match(/class="task-card/g) || []).length,
      marks: (todo.match(/<mark class="kw-hit">/g) || []).length,
      hasTitle: todo.indexOf('<mark class="kw-hit">甲银行</mark>漏洞修复') >= 0,
      count: String(document.getElementById('todoCount').textContent),
      other: (document.getElementById('doneTasks').innerHTML.match(/class="task-card/g) || []).length
    };
  })()`, sandbox);
  check('N19 输入即时过滤（卡片数=命中数，其他列同步为空态）',
    renderRes.cards === 1 && renderRes.other === 0);
  check('N20 卡片标题命中片段被高亮', renderRes.marks >= 1 && renderRes.hasTitle === true);
  check('N21 列头计数随检索变化', renderRes.count === '1');

  const clearRes = vm.runInContext(`(function () {
    clearBoardSearch();
    return {
      kw: boardKeyword,
      cards: (document.getElementById('todoTasks').innerHTML.match(/class="task-card/g) || []).length
    };
  })()`, sandbox);
  check('N22 清空检索恢复全量（待办列 4 张，另 1 张属已完成）',
    clearRes.kw === '' && clearRes.cards === 4);
  check('N23 检索词刻意不持久化（刷新即复位）',
    !Object.keys(store).some(k => /search|keyword/i.test(k)));

  const emptyHtml = vm.runInContext(`(function () {
    boardKeyword = '不存在的词';
    renderTasks('todoTasks', []);
    var h = document.getElementById('todoTasks').innerHTML;
    boardKeyword = '';
    return h;
  })()`, sandbox);
  check('N24 零命中空态带上关键词，便于确认搜的是什么',
    emptyHtml.indexOf('未匹配到「不存在的词」') >= 0);

  const grpRes = vm.runInContext(`(function () {
    boardGroupByProject = true;
    collapsedBoardGroups.add('甲银行');
    boardKeyword = '漏洞';
    currentTasks = __SETS;
    renderBoard();
    var h = document.getElementById('todoTasks').innerHTML;
    var res = { noHide: h.indexOf('display:none') === -1, arrowOpen: h.indexOf('▾') >= 0 };
    boardKeyword = '';
    collapsedBoardGroups.delete('甲银行');
    boardGroupByProject = false;
    renderBoard();
    return res;
  })()`, sandbox);
  check('N25 检索期间忽略分组折叠（否则命中卡片藏在折叠里，看着像搜不到）',
    grpRes.noHide === true && grpRes.arrowOpen === true);

  const headRes = vm.runInContext(`(function () {
    var vb = document.getElementById('view-board');
    vb.classList.remove('active');
    onHeaderSearchInput('农业');
    var res = { active: vb.classList.contains('active'), kw: boardKeyword };
    boardKeyword = ''; syncBoardSearch('all');
    vb.classList.add('active');
    return res;
  })()`, sandbox);
  check('N26 顶栏入口沿用「输入即回看板」的既有肌肉记忆',
    headRes.active === true && headRes.kw === '农业');

  const tblBefore = created.length;
  vm.runInContext(`(function () {
    currentTasks = __SETS;
    boardKeyword = '漏洞';
    boardMode = 'table';
    renderTaskTable();
    boardKeyword = ''; boardMode = 'card';
  })()`, sandbox);
  const tblHtml = created.slice(tblBefore).map(e => e.innerHTML || '').join('');
  check('N27 表格模式同样受检索过滤（并带高亮）',
    tblHtml.indexOf('甲银行') >= 0 && tblHtml.indexOf('乙行现场支持') === -1 &&
    tblHtml.indexOf('<mark class="kw-hit">') >= 0);

  const escKeyRes = vm.runInContext(`(function () {
    boardKeyword = '甲银行';
    onBoardSearchKeydown({ key: 'Escape', stopPropagation: function () {} });
    var cleared = boardKeyword;
    boardKeyword = '甲银行';
    onBoardSearchKeydown({ key: 'a', stopPropagation: function () {} });
    var kept = boardKeyword;
    boardKeyword = '';
    return { cleared: cleared, kept: kept };
  })()`, sandbox);
  check('N28 检索框内 Esc 清空，其他按键不干扰输入',
    escKeyRes.cleared === '' && escKeyRes.kept === '甲银行');

  let capturedUrl = null;
  const realFetch = sandbox.fetch;
  sandbox.fetch = (u) => { capturedUrl = u; return new Promise(() => {}); };
  vm.runInContext('loadTasks()', sandbox);
  sandbox.fetch = realFetch;
  check('N29 loadTasks 恒拉全量（currentTasks 不能被后端预先过滤，否则检索基数就错了）',
    typeof capturedUrl === 'string' && capturedUrl.indexOf('/api/tasks') === 0 &&
    capturedUrl.indexOf('keyword') === -1);

  check('N30 检索控件元素齐全（看板入口 + 顶栏入口 + 命中回显 + 清除）',
    html.indexOf('id="boardSearchInput"') >= 0 && html.indexOf('id="boardSearchClear"') >= 0 &&
    html.indexOf('id="boardSearchHit"') >= 0 && html.indexOf('id="boardToolbar"') >= 0 &&
    html.indexOf('oninput="onHeaderSearchInput(this.value)"') >= 0);
  check('N31 检索样式契约（输入框 / 检索态 / 命中高亮）',
    html.indexOf('.board-search-input{') >= 0 && html.indexOf('mark.kw-hit{') >= 0 &&
    html.indexOf('.board-toolbar.is-searching .board-search-clear{display:flex;}') >= 0 &&
    html.indexOf('.board-toolbar.is-searching .board-search-hit{display:inline-flex;}') >= 0);
  check('N32 「/」聚焦快捷键仅在非输入态、且在看板视图时才触发（源码契约）',
    src.indexOf("e.key === '/'") >= 0 && src.indexOf('focusBoardSearch()') >= 0);

  // ---- O 组：卡片模式工具栏统计（此前只在表格路径调用，卡片模式恒空） ----
  const statRes = vm.runInContext(`(function () {
    var __base = { description: '', priority: 'medium', category: '', link_url: '',
      subtask_total: 0, subtask_done: 0, progress_count: 0, latest_progress: '',
      repeat_freq: 'none', repeat_day: null, kind: 'task', completed_date: '',
      completion_note: '', updated_at: '2026-09-01 00:00:00' };
    function __mk(id, title, status, project, due) {
      var o = JSON.parse(JSON.stringify(__base));
      o.id = id; o.title = title; o.status = status; o.project = project; o.due_date = due;
      return o;
    }
    var __S = [
      __mk(901, '甲银行漏洞修复', 'todo', '甲银行', '2026-09-10'),
      __mk(902, '乙行现场支持', 'todo', '乙银行', '2026-09-25'),
      __mk(903, 'Harmony 策略转换', 'in_progress', '丙银行', ''),
      __mk(904, '年终总结', 'done', '', '2026-09-01')
    ];
    var prevTasks = currentTasks, prevKw = boardKeyword, prevGroup = boardGroupByProject, prevDate = currentDate;
    var prevF = { status: tableFilter.status, project: tableFilter.project, overdue: tableFilter.overdue };
    currentDate = '2026-09-20'; boardGroupByProject = false;
    tableFilter.status = ''; tableFilter.project = ''; tableFilter.overdue = false;
    currentTasks = __S; boardKeyword = '';
    renderBoard();
    var stat = document.getElementById('boardStat');
    var full = String(stat.textContent);
    boardKeyword = '银行';
    renderBoard();
    var searched = String(stat.textContent);
    var todoAfterSearch = String(document.getElementById('todoCount').textContent);
    boardKeyword = '';
    tableFilter.status = 'todo';
    renderBoard();
    var filtered = String(stat.textContent);
    tableFilter.status = '';
    boardMode = 'table';
    renderTaskTable();
    var inTable = String(stat.textContent);
    boardMode = 'card';
    boardKeyword = prevKw; currentTasks = prevTasks; boardGroupByProject = prevGroup; currentDate = prevDate;
    tableFilter.status = prevF.status; tableFilter.project = prevF.project; tableFilter.overdue = prevF.overdue;
    renderBoard();
    return { full: full, searched: searched, filtered: filtered, inTable: inTable,
             todoAfterSearch: todoAfterSearch };
  })()`, sandbox);
  check('O1 卡片模式工具栏统计已填充（此前只在表格路径调用，卡片模式恒为空白）',
    statRes.full === '共 4 项 · 已完成 1 · 逾期 1');
  check('O2 统计口径为全量，不随检索收缩（避免被误读成「任务只剩几条」）',
    statRes.searched === statRes.full && statRes.todoAfterSearch === '2');
  check('O3 统计口径同样不受状态筛选影响（与表格模式一致）',
    statRes.filtered === statRes.full);
  check('O4 表格模式统计未受影响（防回归）',
    statRes.inTable === statRes.full);

  const rbStart = src.indexOf('function renderBoard()');
  const rbEnd = src.indexOf('function syncBoardControls(', rbStart);
  const rbBody = (rbStart >= 0 && rbEnd > rbStart) ? src.slice(rbStart, rbEnd) : '';
  check('O5 卡片渲染路径显式调用 updateBoardStat（源码契约，防止再次漏接）',
    rbBody.indexOf('updateBoardStat(currentTasks)') >= 0);
  check('O6 统计元素由 margin-left:auto 贴右端（样式契约）',
    /\.board-stat\{[^}]*margin-left:auto/.test(html));

  console.log('================ FULL REGRESSION ================');
  results.forEach(([s, n]) => console.log(s + '  ' + n));
  const fails = results.filter(r => r[0] === 'FAIL').length;
  console.log(fails === 0 ? 'RESULT: ALL ' + results.length + ' PASS' : 'RESULT: ' + fails + '/' + results.length + ' FAIL');
  process.exit(fails === 0 ? 0 : 1);
}

main().catch(e => { console.error('REGRESSION ERROR:', e.message); console.error(e.stack); process.exit(1); });
