"""Step 1 单元验证：时间语义归一化 + 重复日期计算（纯函数，不碰数据库）"""
import sys, os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import workbench as w

fails = []


def chk(label, got, exp):
    ok = got == exp
    if not ok:
        fails.append((label, got, exp))
    print(('PASS  ' if ok else 'FAIL  ') + label + ' -> ' + str(got) + ('' if ok else '  (expect ' + str(exp) + ')'))


print('=== _next_repeat_date ===')
chk('daily', w._next_repeat_date('daily', None, date(2026, 9, 11)), date(2026, 9, 12))
chk('weekly 周一(2026-09-11 是周五)', w._next_repeat_date('weekly', 0, date(2026, 9, 11)), date(2026, 9, 14))
chk('monthly 15 当月未过', w._next_repeat_date('monthly', 15, date(2026, 9, 11)), date(2026, 9, 15))
chk('monthly 15 当月已过', w._next_repeat_date('monthly', 15, date(2026, 9, 20)), date(2026, 10, 15))
chk('monthly 31 月底收敛', w._next_repeat_date('monthly', 31, date(2026, 9, 11)), date(2026, 9, 30))
chk('monthly 31 跨月', w._next_repeat_date('monthly', 31, date(2026, 9, 30)), date(2026, 10, 31))
chk('quarterly 20 九月->十月', w._next_repeat_date('quarterly', 20, date(2026, 9, 11), None), date(2026, 10, 20))
chk('quarterly 20 跨年', w._next_repeat_date('quarterly', 20, date(2026, 10, 25), None), date(2027, 1, 20))
chk('quarterly 31 收敛(1月)', w._next_repeat_date('quarterly', 31, date(2026, 9, 1), None), date(2026, 10, 31))
chk('yearly 6-20 跨年', w._next_repeat_date('yearly', 20, date(2026, 9, 11), 6), date(2027, 6, 20))
chk('yearly 6-20 当年', w._next_repeat_date('yearly', 20, date(2026, 3, 1), 6), date(2026, 6, 20))
chk('yearly 2-31 闰月收敛', w._next_repeat_date('yearly', 31, date(2026, 3, 1), 2), date(2027, 2, 28))

print()
print('=== _normalize_kind ===')
chk('routine 合法', w._normalize_kind('routine'), 'routine')
chk('commit 合法', w._normalize_kind('commit'), 'commit')
chk('非法回退', w._normalize_kind('bogus'), 'task')
chk('None 回退', w._normalize_kind(None), 'task')

print()
print('=== _normalize_repeat ===')
chk('节律默认提前 30 天', w._normalize_repeat('monthly', 15, None, 'routine', None), ('monthly', 15, None, 30))
chk('承诺默认提前 3 天', w._normalize_repeat('none', None, None, 'commit', None), ('none', None, None, 3))
chk('计划默认提前 1 天', w._normalize_repeat('none', None, None, 'plan', None), ('none', None, None, 1))
chk('普通任务无默认提醒', w._normalize_repeat('none', None, None, 'task', None), ('none', None, None, 0))
chk('yearly 完整', w._normalize_repeat('yearly', 20, 6, 'routine', None), ('yearly', 20, 6, 30))
chk('非法频率回退', w._normalize_repeat('bogus', None, None, 'task', 5), ('none', None, None, 5))
chk('提前量封顶 30', w._normalize_repeat('none', None, None, 'task', 99), ('none', None, None, 30))
chk('显式 0 覆盖默认', w._normalize_repeat('monthly', 5, None, 'routine', 0), ('monthly', 5, None, 0))
chk('yearly 缺月份回退当前月', w._normalize_repeat('yearly', 5, None, 'routine', None),
    ('yearly', 5, w.beijing_now().month, 30))
chk('weekly 越界收敛', w._normalize_repeat('weekly', 9, None, 'routine', None), ('weekly', 6, None, 30))

print()
if fails:
    print('FAILED: ' + str(len(fails)))
    for f in fails:
        print('   ' + str(f))
    sys.exit(1)
print('ALL PASS')
