"""Step 2 验证：提醒引擎时间轴分组 + 类型化字段 + 计划类父任务聚合（临时库）"""
import sys, os, tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import workbench as w

tmp = tempfile.mkdtemp(prefix='wb_s2_')
w.DB_PATH = os.path.join(tmp, 'test.db')
w.init_db()
c = w.app.test_client()

today = w.beijing_now().date()


def d(n):
    return (today + timedelta(days=n)).isoformat()


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


print('=== 造数据（今天 ' + today.isoformat() + '） ===')
t_late = mk(title='逾期承诺', kind='commit', due_date=d(-2))
t_today = mk(title='今日承诺', kind='commit', due_date=d(0))
t_routine = mk(title='月度巡检交报告', kind='routine', repeat_freq='monthly', repeat_day=15,
               deliverable='巡检报告', project='客户甲', due_date=d(4))
t_year = mk(title='合同年度巡检', kind='routine', repeat_freq='yearly', repeat_month=6,
            repeat_day=20, due_date=d(200))
t_autodate = mk(title='自动补日期的节律', kind='routine', repeat_freq='monthly', repeat_day=1)
t_commit_m = mk(title='月中承诺', kind='commit', due_date=d(24))
t_plain = mk(title='普通无日期任务')
t_plan = mk(title='研发计划项目', kind='plan', project='客户乙')
ch1 = mk(title='需求确认', parent_id=t_plan['id'], due_date=d(-10), status='done')
ch2 = mk(title='联调完成', parent_id=t_plan['id'], due_date=d(4))
ch3 = mk(title='安全测试通过', parent_id=t_plan['id'], due_date=d(40))

r = c.get('/api/reminders')
chk('reminders 200', r.status_code, 200)
data = r.get_json()


def ids(key):
    s = [x for x in data['sections'] if x['key'] == key][0]
    return [i['id'] for i in s['items']]


def find(pid):
    for s in data['sections']:
        for i in s['items']:
            if i['id'] == pid:
                return i
    return None


print()
print('=== 结构 ===')
chk('section 数量', len(data['sections']), 5)
chk('section keys', [s['key'] for s in data['sections']],
    ['now', 'soon', 'month', 'later', 'unscheduled'])
chk('counts 字段齐全',
    all(k in data['counts'] for k in ('overdue', 'today', 'week', 'month', 'later', 'unscheduled', 'window')), True)
chk('兼容字段齐全', all(k in data for k in ('overdue', 'due_today', 'due_soon', 'no_due')), True)

print()
print('=== 分组 ===')
chk('逾期 -> now', t_late['id'] in ids('now'), True)
chk('今日 -> now', t_today['id'] in ids('now'), True)
chk('4 天后 -> soon', t_routine['id'] in ids('soon'), True)
chk('24 天后 -> month', t_commit_m['id'] in ids('month'), True)
chk('200 天后 -> later', t_year['id'] in ids('later'), True)
chk('无日期普通任务 -> unscheduled', t_plain['id'] in ids('unscheduled'), True)
chk('节律自动补日期后不在 unscheduled', t_autodate['id'] in ids('unscheduled'), False)
chk('逾期排在今日之前',
    ids('now').index(t_late['id']) < ids('now').index(t_today['id']), True)

print()
print('=== 计划类父任务聚合 ===')
p = find(t_plan['id'])
chk('父任务出现在时间轴', p is not None, True)
if p:
    chk('父任务锚点 = 最早未完成子节点', p['due_date'], d(4))
    chk('父任务下一节点名', p.get('next_node'), '联调完成')
    chk('父任务阶段进度', p.get('stage_desc'), '1/3 阶段完成')
chk('子任务不单独出现', all(find(x['id']) is None for x in (ch1, ch2, ch3)), True)

print()
print('=== 类型化字段 ===')
it = find(t_routine['id'])
chk('kind', it['kind'], 'routine')
chk('kind_label', it['kind_label'], '节律')
chk('deliverable', it['deliverable'], '巡检报告')
chk('repeat_desc 月频', it['repeat_desc'], '每月 15 日')
chk('remind_advance', it['remind_advance'], 30)
chk('in_window(4 天 <= 30 天窗口)', it['in_window'], True)
chk('年度 repeat_desc', find(t_year['id'])['repeat_desc'], '每年 6 月 20 日')
chk('200 天后未进提醒窗口', find(t_year['id'])['in_window'], False)
chk('逾期天数', find(t_late['id'])['days_late'], 2)
chk('承诺 kind_label', find(t_late['id'])['kind_label'], '承诺')

print()
print('=== 节律自动补日期 ===')
allt = {x['id']: x for x in c.get('/api/tasks').get_json()}
auto = allt.get(t_autodate['id'], {})
chk('due_date 已自动填入', bool(auto.get('due_date')), True)
chk('自动日期可解析', auto.get('due_date', '')[:4].isdigit(), True)
print('     自动填入日期 = ' + str(auto.get('due_date')))

print()
print('=== 兼容字段（看板铃铛） ===')
chk('overdue.count', data['overdue']['count'], 1)
chk('due_today.count', data['due_today']['count'], 1)
chk('due_soon.count == soon 分组', data['due_soon']['count'], len(ids('soon')))
chk('no_due.count == unscheduled 分组', data['no_due']['count'], len(ids('unscheduled')))
chk('兼容项含标题字段', all('title' in i and 'due_date' in i for i in data['overdue']['items']), True)

print()
if fails:
    print('FAILED: ' + str(len(fails)))
    for f in fails:
        print('   ' + str(f))
    sys.exit(1)
print('ALL PASS')
