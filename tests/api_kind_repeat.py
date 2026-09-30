"""Step 1 API 层验证：新字段写入/继承/类型切换（使用临时空库，不碰任何真实数据）"""
import sys, os, tempfile, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import workbench as w

tmp = tempfile.mkdtemp(prefix='wb_test_')
w.DB_PATH = os.path.join(tmp, 'test.db')
w.init_db()
c = w.app.test_client()

fails = []


def chk(label, got, exp):
    ok = got == exp
    if not ok:
        fails.append((label, got, exp))
    print(('PASS  ' if ok else 'FAIL  ') + label + ' -> ' + str(got) + ('' if ok else '  (expect ' + str(exp) + ')'))


def mk(**kw):
    r = c.post('/api/tasks', json=kw)
    assert r.status_code == 200, r.data
    return r.get_json()


print('=== 创建：四类事项 ===')
t_routine = mk(title='月度巡检', kind='routine', repeat_freq='monthly', repeat_day='15',
               deliverable='巡检报告', project='客户甲', due_date='2026-09-15')
chk('节律 kind', t_routine['kind'], 'routine')
chk('节律 freq', t_routine['repeat_freq'], 'monthly')
chk('节律 day', t_routine['repeat_day'], 15)
chk('节律默认提前 30 天', t_routine['remind_advance'], 30)
chk('节律交付物', t_routine['deliverable'], '巡检报告')

t_year = mk(title='合同年度巡检', kind='routine', repeat_freq='yearly', repeat_month='6',
            repeat_day='20', deliverable='年度巡检报告', due_date='2026-06-20')
chk('年频 freq', t_year['repeat_freq'], 'yearly')
chk('年频 month', t_year['repeat_month'], 6)
chk('年频默认提前 30 天', t_year['remind_advance'], 30)

t_quarter = mk(title='季度巡检', kind='routine', repeat_freq='quarterly', repeat_day='20',
               due_date='2026-10-20')
chk('季频 freq', t_quarter['repeat_freq'], 'quarterly')
chk('季频默认提前 30 天', t_quarter['remind_advance'], 30)

t_commit = mk(title='客户要求交付', kind='commit', due_date='2026-09-18')
chk('承诺 kind', t_commit['kind'], 'commit')
chk('承诺默认提前 3 天', t_commit['remind_advance'], 3)

t_plan = mk(title='研发计划项目', kind='plan')
chk('计划 kind', t_plan['kind'], 'plan')
chk('计划默认提前 1 天', t_plan['remind_advance'], 1)

t_plain = mk(title='普通待办')
chk('普通任务默认 kind', t_plain['kind'], 'task')
chk('普通任务默认提前 0', t_plain['remind_advance'], 0)

print()
print('=== 非法输入防护 ===')
t_bad = mk(title='非法类型', kind='nonsense', repeat_freq='hourly')
chk('非法 kind 回退', t_bad['kind'], 'task')
chk('非法 freq 回退', t_bad['repeat_freq'], 'none')

print()
print('=== 节律完成 → 自动滚下一期 ===')
r = c.put('/api/tasks/' + str(t_routine['id']), json={'status': 'done'})
body = r.get_json()
chk('完成返回 200', r.status_code, 200)
chk('生成下一期日期', body.get('generated_next_due'), '2026-10-15')

all_tasks = c.get('/api/tasks').get_json()
nxt = [x for x in all_tasks if x.get('repeat_source_id') == t_routine['id']]
chk('下一期存在', len(nxt), 1)
if nxt:
    n = nxt[0]
    chk('下一期 kind 继承', n['kind'], 'routine')
    chk('下一期 freq 继承', n['repeat_freq'], 'monthly')
    chk('下一期 day 继承', n['repeat_day'], 15)
    chk('下一期交付物继承', n['deliverable'], '巡检报告')
    chk('下一期提前量继承', n['remind_advance'], 30)
    chk('下一期状态', n['status'], 'todo')

print()
print('=== 年度巡检完成（2026-06 已过，补做后滚次年） ===')
r_year = c.put('/api/tasks/' + str(t_year['id']), json={'status': 'done'})
chk('年频下一期', r_year.get_json().get('generated_next_due'), '2027-06-20')

print()
print('=== 类型切换时取新类型默认提前量 ===')
r = c.put('/api/tasks/' + str(t_plain['id']),
          json={'kind': 'routine', 'repeat_freq': 'monthly', 'repeat_day': '8'})
chk('task -> routine 后提前量', r.get_json()['remind_advance'], 30)
r = c.put('/api/tasks/' + str(t_plain['id']), json={'remind_advance': 5})
chk('显式指定后保留 5', r.get_json()['remind_advance'], 5)
r = c.put('/api/tasks/' + str(t_plain['id']), json={'kind': 'commit'})
chk('routine -> commit 取承诺默认', r.get_json()['remind_advance'], 3)

print()
print('=== 任务列表排序：优先级必须按语义权重，不能按字符串降序 ===')
mk(title='排序样例-low', priority='low')
mk(title='排序样例-medium', priority='medium')
mk(title='排序样例-high', priority='high')
rows = c.get('/api/tasks').get_json()
titles = [t['title'] for t in rows if t['title'].startswith('排序样例-')]
# 直接 ORDER BY priority DESC 会得到 medium > low > high（SQLite 字符串降序），高优先级会沉底
chk('列表顺序 high -> medium -> low', titles,
    ['排序样例-high', '排序样例-medium', '排序样例-low'])

print()
print('=== 旧数据兼容（无 kind 的历史任务） ===')
r = c.put('/api/tasks/' + str(t_commit['id']), json={'title': '客户要求交付（改名）'})
chk('PUT 不传 kind 保持原值', r.get_json()['kind'], 'commit')
chk('PUT 不传提前量保持原值', r.get_json()['remind_advance'], 3)

print()
print('临时库: ' + tmp)
if fails:
    print('FAILED: ' + str(len(fails)))
    for f in fails:
        print('   ' + str(f))
    sys.exit(1)
print('ALL PASS')
