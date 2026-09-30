"""
个人工作台 - Personal Workbench
轻量级任务管理 + 项目填报链接管理
SQLite 单文件数据库，Flask Web 界面
"""

import sqlite3
import os
import json
import shutil
import re
import calendar
from datetime import datetime, date, timedelta, timezone
from flask import Flask, render_template, request, jsonify, Response

import sys
import io
import threading


def beijing_now():
    """返回北京时间（UTC+8），独立于运行机器系统时区配置，彻底避免时区错乱导致日期整体偏移。"""
    return datetime.utcnow() + timedelta(hours=8)


def beijing_today():
    """返回北京时间日期字符串 YYYY-MM-DD。"""
    return beijing_now().date().isoformat()


# ============ 时间语义（kind）与重复频率常量 ============
# 四类日常事项归为三种时间语义：
#   routine 节律 —— 该做这件事的日子（每月/每季/每年巡检），完成即滚下一期
#   commit  承诺 —— 对客户的死线，逾期即事故，需填原因
#   plan    计划 —— 研发给的基线，滑期记为变更，不报红
KIND_VALUES = ("task", "routine", "commit", "plan")
REPEAT_FREQS = ("none", "daily", "weekly", "monthly", "quarterly", "yearly")
REPEAT_FREQ_SQL = ",".join(f"'{f}'" for f in REPEAT_FREQS)
ROUTINE_DEFAULT_ADVANCE = 30   # 节律类默认提前提醒天数（巡检需提前一个月启动）
COMMIT_DEFAULT_ADVANCE = 3     # 承诺类默认提前提醒天数
PLAN_DEFAULT_ADVANCE = 1       # 计划类默认提前提醒天数
QUARTER_MONTHS = (1, 4, 7, 10)  # 自然季度起始月

# 优先级排序权重：不能用 priority DESC —— SQLite 按字符串降序会得到 medium > low > high，
# 高优先级反而沉底。必须用 CASE 映射成语义权重。
PRIORITY_ORDER_SQL = ("CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
                      "WHEN 'low' THEN 2 ELSE 1 END")


def _kind_default_advance(kind):
    """按时间语义返回默认提前提醒天数。"""
    if kind == "routine":
        return ROUTINE_DEFAULT_ADVANCE
    if kind == "commit":
        return COMMIT_DEFAULT_ADVANCE
    if kind == "plan":
        return PLAN_DEFAULT_ADVANCE
    return 0


def _row_get(row, key, default=None):
    """从 sqlite3.Row 安全取值（列不存在或为 NULL 时返回 default）。"""
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _normalize_kind(kind):
    """时间语义白名单校验，非法值回退 task（普通待办）。"""
    k = (kind or "task").strip().lower()
    return k if k in KIND_VALUES else "task"


def _normalize_repeat(repeat_freq, repeat_day, repeat_month, kind, remind_advance):
    """归一化重复与提醒字段，返回 (freq, day, month, advance)。

    remind_advance 传 None 表示"调用方未提供"，此时按 kind 取默认提前量
    （节律 30 天 / 承诺 3 天 / 计划 1 天 / 普通任务 0 天）。
    repeat_day=31 视为月底；yearly 缺 repeat_month 时回退到当前月。
    """
    freq = (repeat_freq or "none").strip().lower()
    if freq not in REPEAT_FREQS:
        freq = "none"

    try:
        day = int(repeat_day) if repeat_day not in (None, "") else None
    except (TypeError, ValueError):
        day = None
    if day is not None:
        if freq == "weekly":
            day = max(0, min(6, day))
        elif freq in ("monthly", "quarterly", "yearly"):
            day = max(1, min(31, day))

    try:
        month = int(repeat_month) if repeat_month not in (None, "") else None
    except (TypeError, ValueError):
        month = None
    if month is not None:
        month = max(1, min(12, month))
    if freq == "yearly" and month is None:
        month = beijing_now().month

    if remind_advance is None:
        adv = _kind_default_advance(kind)
    else:
        try:
            adv = max(0, min(30, int(remind_advance)))
        except (TypeError, ValueError):
            adv = _kind_default_advance(kind)

    return freq, day, month, adv


def _next_repeat_date(freq, repeat_day, base_date, repeat_month=None):
    """重复任务的下一期应发生日期，严格晚于 base_date。
    daily: 次日；weekly: 下一个指定的周几；monthly: 下一个指定的每月几号
    quarterly: 下一个自然季度月（1/4/7/10 月）的指定日；
    yearly: 下一年的 repeat_month 月 repeat_day 日；
    （repeat_day=31 一律视为月底，自动收敛到当月最后一天）。"""
    if freq == "daily":
        return base_date + timedelta(days=1)
    if freq == "weekly":
        wd = (repeat_day if repeat_day is not None else 0) % 7
        d = base_date + timedelta(days=1)
        while d.weekday() != wd:
            d += timedelta(days=1)
        return d
    if freq == "monthly":
        md = repeat_day if repeat_day is not None else 1
        d = base_date + timedelta(days=1)
        while True:
            last = calendar.monthrange(d.year, d.month)[1]
            candidate = d.replace(day=min(md, last))
            if candidate >= d:
                return candidate
            d = d.replace(day=last) + timedelta(days=1)
    if freq == "quarterly":
        md = repeat_day if repeat_day is not None else 1
        d = base_date + timedelta(days=1)
        for _ in range(400):  # 安全上限：最多向后找一年
            if d.month in QUARTER_MONTHS:
                last = calendar.monthrange(d.year, d.month)[1]
                candidate = d.replace(day=min(md, last))
                if candidate >= d:
                    return candidate
            d = d.replace(day=calendar.monthrange(d.year, d.month)[1]) + timedelta(days=1)
        return None
    if freq == "yearly":
        mm = repeat_month if repeat_month else 1
        md = repeat_day if repeat_day is not None else 1
        for delta_year in (0, 1, 2):
            y = base_date.year + delta_year
            last = calendar.monthrange(y, mm)[1]
            try:
                candidate = base_date.replace(year=y, month=mm, day=min(md, last))
            except ValueError:
                continue
            if candidate > base_date:
                return candidate
        return None
    return None


def quarter_start(s):
    """'2026-Q3' -> '2026-07-01'"""
    y, q = s.split("-Q")
    m = (int(q) - 1) * 3 + 1
    return f"{y}-{m:02d}-01"


def quarter_end(s):
    """'2026-Q3' -> '2026-09-30'"""
    y, q = s.split("-Q")
    m = int(q) * 3
    last = calendar.monthrange(int(y), m)[1]
    return f"{y}-{m:02d}-{last:02d}"


def _ensure_utf8_stream(stream, name='out'):
    """控制台编码处理，支持 emoji 等 UTF-8 字符；noconsole 模式下 stream 可能为 None。"""
    if stream is None:
        # 无控制台时把输出写到 exe 所在目录的日志文件，方便排错
        try:
            log_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, f'workbench_{name}.log')
            f = open(log_path, 'a', encoding='utf-8', buffering=1)
            f.write(f"\n===== {datetime.now().isoformat()} =====\n")
            f.flush()
            return f
        except Exception:
            return io.StringIO()
    encoding = getattr(stream, 'encoding', None)
    if encoding and 'utf-8' in encoding.lower():
        return stream
    try:
        return io.TextIOWrapper(stream.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, TypeError):
        return stream


sys.stdout = _ensure_utf8_stream(sys.stdout, 'out')
sys.stderr = _ensure_utf8_stream(sys.stderr, 'err')


# 兼容 PyInstaller 单文件打包：冻结后 __file__ 指向临时解压目录，
# 数据须落在 exe 真实所在目录；模板由 --add-data 内嵌于 sys._MEIPASS。
# 注意：部分 PyInstaller 构建不设置 sys.frozen，但一定会设置 sys._MEIPASS，故以它为准。
IS_FROZEN = getattr(sys, 'frozen', False) or hasattr(sys, '_MEIPASS')
if IS_FROZEN:
    # onefile 模式：sys.executable 指向临时解压目录，须用 argv[0] 取用户实际运行的 exe 位置
    BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
    TEMPLATE_DIR = os.path.join(sys._MEIPASS, 'templates')
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)

# 数据库文件路径（基于 exe 真实目录，打包/跨机后数据持久化）
DB_PATH = os.path.join(BASE_DIR, "workbench.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if IS_FROZEN:
        # 打包后使用单文件 journal 模式，避免 WAL 在复制/跨机时数据不可见
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            pass
    else:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """初始化数据库表"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    
    conn = get_db()
    cursor = conn.cursor()
    
    # 任务表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT "",
            status TEXT DEFAULT "todo" CHECK(status IN ("todo", "in_progress", "done")),
            priority TEXT DEFAULT "medium" CHECK(priority IN ("high", "medium", "low")),
            project TEXT DEFAULT "",
            category TEXT DEFAULT "",
            created_date TEXT NOT NULL,
            due_date TEXT DEFAULT "",
            link_url TEXT DEFAULT "",
            is_recurring INTEGER DEFAULT 0,
            completed_date TEXT DEFAULT "",
            completion_note TEXT DEFAULT "",
            parent_id INTEGER DEFAULT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    
    # 项目填报链接表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            url TEXT DEFAULT "",
            description TEXT DEFAULT "",
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 每日工作日志表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 便利贴表（联系人 / 流程 / 其他 碎片信息）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT DEFAULT "其他" CHECK(category IN ("联系人", "流程", "其他")),
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 任务进展记录表（追加式过程记录，只增不改，保证可追溯）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            type TEXT DEFAULT "其他" CHECK(type IN ("研发反馈", "方案变更", "里程碑", "风险", "其他")),
            content TEXT NOT NULL,
            attachment_url TEXT DEFAULT "",
            created_by TEXT DEFAULT "",
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)

    # 每日流水账表（按真实时间戳记录，供述职 / 复盘数据源）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chronicle (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT DEFAULT "工作" CHECK(category IN ("工作", "会议", "阻塞", "其他")),
            entry_date TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ============ 数据库迁移：为旧库补充新增字段 ============
    # 检查 tasks 表已有列
    cursor.execute("PRAGMA table_info(tasks)")
    existing_cols = [row[1] for row in cursor.fetchall()]

    migration_fields = [
        ("completion_note", "TEXT DEFAULT ''"),
        ("parent_id", "INTEGER DEFAULT NULL"),
        ("repeat_freq", "TEXT DEFAULT 'none'"),
        ("repeat_day", "INTEGER"),
        ("remind_advance", "INTEGER DEFAULT 0"),
        ("repeat_source_id", "INTEGER DEFAULT NULL"),
        # 里程碑体系：时间语义 + 年频月份 + 交付物
        ("kind", "TEXT DEFAULT 'task'"),
        ("repeat_month", "INTEGER"),
        ("deliverable", "TEXT DEFAULT ''"),
    ]
    for col_name, col_def in migration_fields:
        if col_name not in existing_cols:
            try:
                cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
                print(f"  ✅ 已迁移字段: tasks.{col_name}")
            except Exception as e:
                print(f"  ⚠️ 迁移字段失败 tasks.{col_name}: {e}")

    # 周期交付（routine_*）体系已由任务原生重复取代：仅在确认空表后清理，绝不动有数据的表
    try:
        for t in ("routine_logs", "routine_tasks"):
            cnt = cursor.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if cnt == 0:
                cursor.execute(f"DROP TABLE IF EXISTS {t}")
                print(f"  🧹 已清理空表: {t}")
            else:
                print(f"  ⚠️ {t} 表含 {cnt} 条数据，保留未删除")
    except Exception:
        pass

    conn.commit()
    conn.close()

def auto_backup():
    """每天首次启动时自动备份数据库"""
    today = beijing_today()
    backup_file = os.path.join(BACKUP_DIR, f"{today}.db")
    if not os.path.exists(backup_file) and os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_file)
        print(f"  ✅ 数据库已备份: {backup_file}")

# 初始化
init_db()
auto_backup()

# ==================== 页面路由 ====================

@app.route("/")
def index():
    return render_template("index.html")

# ==================== 任务 API ====================

@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    """获取任务列表（看板为单一事实源，按状态展示全部任务，不再按日期门控/滚动）"""
    status = request.args.get("status", "")
    project = request.args.get("project", "")
    keyword = request.args.get("keyword", "")
    parent_id = request.args.get("parent_id", "")

    conn = get_db()
    cursor = conn.cursor()

    conditions = []
    params = []

    # parent_id 过滤：top 表示仅顶层任务；数字表示指定父任务；其余表示不限制
    if parent_id == "top":
        conditions.append("(parent_id IS NULL OR parent_id = 0)")
    elif parent_id.isdigit():
        conditions.append("parent_id = ?")
        params.append(int(parent_id))

    if status:
        conditions.append("status = ?")
        params.append(status)

    if project:
        conditions.append("project LIKE ?")
        params.append(f"%{project}%")

    if keyword:
        conditions.append("(title LIKE ? OR description LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    query = f"""SELECT *, 
        (SELECT COUNT(*) FROM tasks c WHERE c.parent_id = tasks.id) AS subtask_total,
        (SELECT COUNT(*) FROM tasks c WHERE c.parent_id = tasks.id AND c.status = 'done') AS subtask_done,
        (SELECT COUNT(*) FROM task_progress p WHERE p.task_id = tasks.id) AS progress_count,
        (SELECT content FROM task_progress p WHERE p.task_id = tasks.id ORDER BY id DESC LIMIT 1) AS latest_progress
        FROM tasks WHERE {where_clause} ORDER BY sort_order ASC, {PRIORITY_ORDER_SQL} ASC, created_at DESC"""

    cursor.execute(query, params)
    tasks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(tasks)

@app.route("/api/tasks", methods=["POST"])
def add_task():
    """添加新任务"""
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    
    now_bj = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    kind = _normalize_kind(data.get("kind"))
    repeat_freq, repeat_day, repeat_month, remind_advance = _normalize_repeat(
        data.get("repeat_freq", "none"),
        data.get("repeat_day"),
        data.get("repeat_month"),
        kind,
        data.get("remind_advance"),
    )
    # 节律类未填日期时，按重复规则自动推算首个发生日（避免落进"待排期"池）
    due_date = (data.get("due_date") or "").strip()
    if not due_date and kind == "routine" and repeat_freq != "none":
        nd = _next_repeat_date(repeat_freq, repeat_day, beijing_now().date(), repeat_month)
        if nd is not None:
            due_date = nd.isoformat()
    # 子任务（计划类项目的阶段节点）：校验父任务存在
    try:
        parent_id = int(data.get("parent_id")) if data.get("parent_id") not in (None, "") else None
    except (TypeError, ValueError):
        parent_id = None
    if parent_id is not None:
        if not cursor.execute("SELECT id FROM tasks WHERE id = ?", (parent_id,)).fetchone():
            parent_id = None
    cursor.execute("""
        INSERT INTO tasks (title, description, status, priority, project, category,
                          created_date, created_at, updated_at, due_date, link_url, is_recurring,
                          repeat_freq, repeat_day, remind_advance,
                          kind, repeat_month, deliverable, parent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("title", "").strip(),
        data.get("description", "").strip(),
        data.get("status", "todo"),
        data.get("priority", "medium"),
        data.get("project", "").strip(),
        data.get("category", ""),
        beijing_today(),
        now_bj,
        now_bj,
        due_date,
        data.get("link_url", "").strip(),
        1 if data.get("is_recurring") else 0,
        repeat_freq,
        repeat_day,
        remind_advance,
        kind,
        repeat_month,
        (data.get("deliverable") or "").strip(),
        parent_id
    ))
    
    conn.commit()
    task_id = cursor.lastrowid
    task = dict(cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    conn.close()
    return jsonify(task)

@app.route("/api/tasks/<int:task_id>", methods=["GET", "PUT", "DELETE"])
def task_item(task_id):
    """单任务路由：合并 GET/PUT/DELETE 到同一装饰器，避免 Flask 多装饰器同 URL 时丢失 GET 方法。"""
    m = request.method
    if m == "GET":
        return _get_task(task_id)
    if m == "PUT":
        return _update_task(task_id)
    return _delete_task(task_id)


def _get_task(task_id):
    """获取单个任务（返回与列表一致的数组格式，供编辑弹窗使用）"""
    conn = get_db()
    cursor = conn.cursor()
    row = cursor.execute("""SELECT *,
        (SELECT COUNT(*) FROM tasks c WHERE c.parent_id = tasks.id) AS subtask_total,
        (SELECT COUNT(*) FROM tasks c WHERE c.parent_id = tasks.id AND c.status = 'done') AS subtask_done,
        (SELECT COUNT(*) FROM task_progress p WHERE p.task_id = tasks.id) AS progress_count,
        (SELECT content FROM task_progress p WHERE p.task_id = tasks.id ORDER BY id DESC LIMIT 1) AS latest_progress
        FROM tasks WHERE id = ?""", (task_id,)).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "task not found"}), 404
    return jsonify([dict(row)])

def _update_task(task_id):
    """更新任务"""
    data = request.json
    conn = get_db()
    cursor = conn.cursor()

    # 先取出现有记录，缺失字段回退到原值（避免 CHECK 约束因空值报错）
    existing = cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "任务不存在"}), 404

    # 计算完成日期与备注
    new_status = data.get("status", existing["status"])
    if new_status == "done":
        completed_date = data.get("completed_date") or beijing_today()
    else:
        completed_date = ""
    completion_note = data.get("completion_note", existing["completion_note"] or "")

    # 子任务若被恢复为未完成，清空完成备注与日期
    if new_status in ("todo", "in_progress") and existing["parent_id"]:
        completion_note = ""

    now_bj = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    # 时间语义 + 重复字段（缺省回退原值，非法值回退安全默认）
    prev_kind = _row_get(existing, "kind", "task")
    new_kind = _normalize_kind(data.get("kind", prev_kind))
    if "remind_advance" in data:
        adv_in = data.get("remind_advance")
    elif new_kind != prev_kind:
        adv_in = None  # 类型变更且未显式指定提前量 → 取新类型的默认值
    else:
        adv_in = _row_get(existing, "remind_advance", 0)

    new_repeat_freq, new_repeat_day, new_repeat_month, new_remind_advance = _normalize_repeat(
        data.get("repeat_freq", _row_get(existing, "repeat_freq", "none")),
        data.get("repeat_day", _row_get(existing, "repeat_day", None)),
        data.get("repeat_month", _row_get(existing, "repeat_month", None)),
        new_kind,
        adv_in,
    )

    cursor.execute("""
        UPDATE tasks SET
            title=?, description=?, status=?, priority=?, project=?,
            category=?, due_date=?, link_url=?, is_recurring=?,
            completed_date=?, completion_note=?, updated_at=?,
            repeat_freq=?, repeat_day=?, remind_advance=?,
            kind=?, repeat_month=?, deliverable=?
        WHERE id=?
    """, (
        data.get("title", existing["title"]),
        data.get("description", existing["description"]),
        new_status,
        data.get("priority", existing["priority"]),
        data.get("project", existing["project"]),
        data.get("category", existing["category"]),
        data.get("due_date", existing["due_date"]),
        data.get("link_url", existing["link_url"]),
        1 if data.get("is_recurring", existing["is_recurring"]) else 0,
        completed_date,
        completion_note,
        now_bj,
        new_repeat_freq,
        new_repeat_day,
        new_remind_advance,
        new_kind,
        new_repeat_month,
        (data.get("deliverable", _row_get(existing, "deliverable", "")) or "").strip(),
        task_id
    ))

    # 重复任务完成 → 自动生成下一期（以 repeat_source_id 防重复生成）
    generated_next = None
    if new_status == "done" and new_repeat_freq in ("daily", "weekly", "monthly", "quarterly", "yearly"):
        already = cursor.execute(
            "SELECT id FROM tasks WHERE repeat_source_id = ?", (task_id,)
        ).fetchone()
        if not already:
            due_raw = existing["due_date"] or beijing_today()
            try:
                base = datetime.strptime(due_raw, "%Y-%m-%d").date()
            except ValueError:
                base = beijing_now().date()
            if base < beijing_now().date():
                base = beijing_now().date()
            nd = _next_repeat_date(new_repeat_freq, new_repeat_day, base, new_repeat_month)
            if nd is not None:
                cursor.execute("""
                    INSERT INTO tasks (title, description, status, priority, project, category,
                                      created_date, created_at, updated_at, due_date, link_url, is_recurring,
                                      repeat_freq, repeat_day, remind_advance, repeat_source_id,
                                      kind, repeat_month, deliverable)
                    VALUES (?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    existing["title"], existing["description"], existing["priority"],
                    existing["project"], existing["category"], beijing_today(),
                    now_bj, now_bj, nd.isoformat(), existing["link_url"],
                    new_repeat_freq, new_repeat_day, new_remind_advance, task_id,
                    new_kind, new_repeat_month, _row_get(existing, "deliverable", "")
                ))
                generated_next = nd.isoformat()
                print(f"  🔁 重复任务 #{task_id} 完成已生成下一期（{new_repeat_freq}，下一次 {nd.isoformat()}）")

    conn.commit()
    task = dict(cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    if generated_next:
        task["generated_next_due"] = generated_next
    conn.close()
    return jsonify(task)

def _delete_task(task_id):
    """删除任务"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/tasks/reorder", methods=["POST"])
def reorder_tasks():
    """重新排序"""
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    for i, task_id in enumerate(data.get("order", [])):
        cursor.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (i, task_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ==================== 任务进展记录 API ====================

@app.route("/api/tasks/<int:task_id>/progress", methods=["GET"])
def get_task_progress(task_id):
    """获取某任务的进展记录（按时间倒序，最新在前）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM task_progress WHERE task_id = ? ORDER BY id DESC",
        (task_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/tasks/<int:task_id>/progress", methods=["POST"])
def add_task_progress(task_id):
    """为任务新增一条进展记录"""
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "进展内容不能为空"}), 400
    ptype = data.get("type", "其他")
    if ptype not in ("研发反馈", "方案变更", "里程碑", "风险", "其他"):
        ptype = "其他"
    attachment = (data.get("attachment_url") or "").strip()
    created_by = (data.get("created_by") or "").strip()
    conn = get_db()
    cursor = conn.cursor()
    existing = cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "任务不存在"}), 404
    now_bj = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO task_progress (task_id, type, content, attachment_url, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, ptype, content, attachment, created_by, now_bj),
    )
    conn.commit()
    pid = cursor.lastrowid
    row = dict(cursor.execute("SELECT * FROM task_progress WHERE id = ?", (pid,)).fetchone())
    conn.close()
    return jsonify(row)

@app.route("/api/tasks/progress/<int:progress_id>", methods=["DELETE"])
def delete_task_progress(progress_id):
    """删除一条进展记录"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_progress WHERE id = ?", (progress_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ==================== 项目填报链接 API ====================

@app.route("/api/project-links", methods=["GET"])
def get_project_links():
    """获取项目填报链接列表"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM project_links ORDER BY sort_order ASC, project_name ASC")
    links = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(links)

@app.route("/api/project-links", methods=["POST"])
def add_project_link():
    """添加项目填报链接"""
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO project_links (project_name, url, description, sort_order)
        VALUES (?, ?, ?, ?)
    """, (
        data.get("project_name", "").strip(),
        data.get("url", "").strip(),
        data.get("description", "").strip(),
        data.get("sort_order", 0)
    ))
    conn.commit()
    link_id = cursor.lastrowid
    link = dict(cursor.execute("SELECT * FROM project_links WHERE id = ?", (link_id,)).fetchone())
    conn.close()
    return jsonify(link)

@app.route("/api/project-links/<int:link_id>", methods=["PUT"])
def update_project_link(link_id):
    """更新项目填报链接"""
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE project_links SET project_name=?, url=?, description=?, sort_order=?
        WHERE id=?
    """, (
        data.get("project_name", ""),
        data.get("url", ""),
        data.get("description", ""),
        data.get("sort_order", 0),
        link_id
    ))
    conn.commit()
    link = dict(cursor.execute("SELECT * FROM project_links WHERE id = ?", (link_id,)).fetchone())
    conn.close()
    return jsonify(link)

@app.route("/api/project-links/<int:link_id>", methods=["DELETE"])
def delete_project_link(link_id):
    """删除项目填报链接"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM project_links WHERE id = ?", (link_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ==================== 每日工作日志 API ====================

@app.route("/api/daily-logs", methods=["GET"])
def get_daily_logs():
    """获取某日工作日志（按时间倒序）"""
    log_date = request.args.get("date", date.today().isoformat())
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM daily_logs WHERE log_date = ? ORDER BY id DESC", (log_date,))
    logs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(logs)

@app.route("/api/daily-logs", methods=["POST"])
def add_daily_log():
    """添加工作日志"""
    data = request.json
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "日志内容不能为空"}), 400
    log_date = data.get("date", date.today().isoformat())
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO daily_logs (log_date, content) VALUES (?, ?)", (log_date, content))
    conn.commit()
    log_id = cursor.lastrowid
    log = dict(cursor.execute("SELECT * FROM daily_logs WHERE id = ?", (log_id,)).fetchone())
    conn.close()
    return jsonify(log)

@app.route("/api/daily-logs/<int:log_id>", methods=["PUT"])
def update_daily_log(log_id):
    """编辑工作日志"""
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    existing = cursor.execute("SELECT * FROM daily_logs WHERE id = ?", (log_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "日志不存在"}), 404
    content = (data.get("content") or "").strip() or existing["content"]
    log_date = data.get("date", existing["log_date"])
    cursor.execute("UPDATE daily_logs SET content = ?, log_date = ? WHERE id = ?", (content, log_date, log_id))
    conn.commit()
    log = dict(cursor.execute("SELECT * FROM daily_logs WHERE id = ?", (log_id,)).fetchone())
    conn.close()
    return jsonify(log)

@app.route("/api/daily-logs/<int:log_id>", methods=["DELETE"])
def delete_daily_log(log_id):
    """删除工作日志"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM daily_logs WHERE id = ?", (log_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/today", methods=["GET"])
def get_today():
    """返回服务端北京时间（YYYY-MM-DD），作为前端“今天”的唯一权威来源。
    使用 UTC+8 计算，不受运行机器系统时区配置异常影响。"""
    return jsonify({"today": beijing_today()})


# ==================== 任务提醒聚合（里程碑） ====================
WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
KIND_LABELS = {"routine": "节律", "commit": "承诺", "plan": "计划", "task": "任务"}


def _repeat_desc(freq, day, month):
    """把重复规则翻译成一句人话，供提醒页展示。"""
    if freq == "daily":
        return "每天"
    if freq == "weekly":
        return "每周" + WEEK_CN[(day or 0) % 7]
    if freq == "monthly":
        return "每月月底" if day == 31 else "每月 %d 日" % (day or 1)
    if freq == "quarterly":
        return "每季度 %d 日" % (day or 1)
    if freq == "yearly":
        return "每年 %d 月 %d 日" % (month or 1, day or 1)
    return ""


def _task_view(t, today):
    """任务行 → 提醒页视图：时间语义、交付物、重复描述、剩余/逾期天数。"""
    kind = t.get("kind") or "task"
    if kind not in KIND_VALUES:
        kind = "task"
    due = (t.get("due_date") or "").strip()
    days_left = None
    if due:
        try:
            days_left = (datetime.strptime(due, "%Y-%m-%d").date() - today).days
        except ValueError:
            due = ""
    advance = t.get("remind_advance") or 0
    if not advance:
        advance = _kind_default_advance(kind)
    return {
        "id": t["id"],
        "title": t["title"],
        "project": t.get("project") or "",
        "priority": t.get("priority") or "medium",
        "status": t.get("status"),
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, "任务"),
        "deliverable": t.get("deliverable") or "",
        "due_date": due,
        "repeat_desc": _repeat_desc(t.get("repeat_freq") or "none",
                                    t.get("repeat_day"), t.get("repeat_month")),
        "remind_advance": advance,
        "days_left": days_left,
        "days_late": -days_left if (days_left is not None and days_left < 0) else 0,
        "in_window": days_left is not None and days_left <= advance,
    }


@app.route("/api/reminders", methods=["GET"])
def api_reminders():
    """里程碑提醒聚合端点。按时间语义与时间轴分组：
    - now         需要立刻处理（已逾期 + 今日）
    - soon        未来 7 天
    - month       未来 30 天
    - later       更远（含年度合同事项）
    - unscheduled 待排期（未设日期）

    计划类父任务聚合其未完成子节点（取最早节点作时间轴锚点，附阶段进度）。
    同时保留 overdue / due_today / due_soon / no_due 字段，供看板铃铛与桌面通知继续使用。
    """
    today = beijing_now().date()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, project, priority, status, due_date, parent_id,
               kind, deliverable, repeat_freq, repeat_day, repeat_month, remind_advance
        FROM tasks WHERE status != 'done'
    """)
    open_rows = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT id, parent_id, status, due_date, title FROM tasks WHERE parent_id IS NOT NULL")
    child_rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    # 阶段进度（含已完成子节点，故用全量 child_rows）
    progress = {}
    for ch in child_rows:
        st = progress.setdefault(ch["parent_id"], {"total": 0, "done": 0})
        st["total"] += 1
        if ch["status"] == "done":
            st["done"] += 1

    parents = {r["id"] for r in open_rows if r["id"] in progress}

    # 父任务：用最早未完成子节点作为时间轴锚点
    anchor = {}
    for pid in parents:
        for ch in child_rows:
            if ch["parent_id"] != pid or ch["status"] == "done":
                continue
            d = (ch["due_date"] or "").strip()
            if not d:
                continue
            if pid not in anchor or d < anchor[pid][0]:
                anchor[pid] = (d, ch["title"])

    items = []
    for r in open_rows:
        if r.get("parent_id") and r["parent_id"] in parents:
            continue  # 子节点由父任务聚合展示，不单独出现
        view = _task_view(r, today)
        if r["id"] in anchor:
            view["due_date"], view["next_node"] = anchor[r["id"]]
            try:
                dl = (datetime.strptime(view["due_date"], "%Y-%m-%d").date() - today).days
                view["days_left"] = dl
                view["days_late"] = -dl if dl < 0 else 0
                view["in_window"] = dl <= view["remind_advance"]
            except ValueError:
                pass
        st = progress.get(r["id"])
        if st:
            view["stage_done"] = st["done"]
            view["stage_total"] = st["total"]
            view["stage_desc"] = "%d/%d 阶段完成" % (st["done"], st["total"])
        items.append(view)

    prio_rank = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 99999,
                              prio_rank.get(x["priority"], 1), -x["id"]))

    overdue = [i for i in items if i["days_left"] is not None and i["days_left"] < 0]
    due_today = [i for i in items if i["days_left"] == 0]
    soon = [i for i in items if i["days_left"] is not None and 1 <= i["days_left"] <= 7]
    month = [i for i in items if i["days_left"] is not None and 8 <= i["days_left"] <= 30]
    later = [i for i in items if i["days_left"] is not None and i["days_left"] > 30]
    unscheduled = [i for i in items if i["days_left"] is None]

    sections = [
        {"key": "now", "label": "需要立刻处理", "hint": "逾期与今日到期", "items": overdue + due_today},
        {"key": "soon", "label": "未来 7 天", "hint": "临近，提前安排", "items": soon},
        {"key": "month", "label": "未来 30 天", "hint": "已进入提醒窗口", "items": month},
        {"key": "later", "label": "更远", "hint": "含年度合同事项", "items": later},
        {"key": "unscheduled", "label": "待排期", "hint": "未设日期，点开补设", "items": unscheduled},
    ]

    return jsonify({
        "today": today.isoformat(),
        "counts": {
            "overdue": len(overdue),
            "today": len(due_today),
            "week": len(soon),
            "month": len(month),
            "later": len(later),
            "unscheduled": len(unscheduled),
            "window": sum(1 for i in items if i["in_window"]),
        },
        "sections": sections,
        # ---- 兼容字段：看板铃铛与桌面通知继续使用 ----
        "overdue": {"count": len(overdue), "items": overdue},
        "due_today": {"count": len(due_today), "items": due_today},
        "due_soon": {"count": len(soon), "items": soon},
        "no_due": {"count": len(unscheduled), "items": unscheduled},
    })

# ==================== 便利贴 ====================
@app.route("/api/notes", methods=["GET"])
def get_notes():
    """获取便利贴列表"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM notes ORDER BY sort_order ASC, id DESC")
    notes = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(notes)

@app.route("/api/notes", methods=["POST"])
def add_note():
    """新增便利贴"""
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"success": False, "error": "内容不能为空"}), 400
    category = data.get("category", "其他")
    if category not in ("联系人", "流程", "其他"):
        category = "其他"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO notes (content, category, sort_order) VALUES (?, ?, ?)",
        (content, category, 0),
    )
    note_id = cursor.lastrowid
    conn.commit()
    note = dict(cursor.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())
    conn.close()
    return jsonify(note)

@app.route("/api/notes/<int:note_id>", methods=["PUT"])
def update_note(note_id):
    """更新便利贴"""
    data = request.json or {}
    conn = get_db()
    cursor = conn.cursor()
    existing = cursor.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"success": False, "error": "便利贴不存在"}), 404
    content = (data.get("content") or "").strip() or existing["content"]
    category = data.get("category", existing["category"])
    if category not in ("联系人", "流程", "其他"):
        category = existing["category"]
    cursor.execute(
        "UPDATE notes SET content=?, category=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (content, category, note_id),
    )
    conn.commit()
    note = dict(cursor.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())
    conn.close()
    return jsonify(note)

@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
def delete_note(note_id):
    """删除便利贴"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ==================== 每日流水账（chronicle）API ====================
@app.route("/api/chronicle", methods=["GET"])
def get_chronicle():
    """获取流水账，按真实时间倒序；支持 start/end（YYYY-MM-DD 或 YYYY-Qn / YYYY）与关键词 q。"""
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()
    q = (request.args.get("q") or "").strip()

    if re.match(r"^\d{4}-Q[1-4]$", start):
        start = quarter_start(start)
    if re.match(r"^\d{4}-Q[1-4]$", end):
        end = quarter_end(end)
    if re.match(r"^\d{4}$", start):
        start = f"{start}-01-01"
    if re.match(r"^\d{4}$", end):
        end = f"{end}-12-31"

    conn = get_db()
    cursor = conn.cursor()
    conditions = []
    params = []
    if start:
        conditions.append("entry_date >= ?")
        params.append(start)
    if end:
        conditions.append("entry_date <= ?")
        params.append(end)
    if q:
        conditions.append("content LIKE ?")
        params.append(f"%{q}%")
    where = " AND ".join(conditions) if conditions else "1=1"
    cursor.execute(f"SELECT * FROM chronicle WHERE {where} ORDER BY created_at DESC, id DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/chronicle", methods=["POST"])
def add_chronicle():
    """新增一条流水账，entry_date 由服务端真实北京时间写入。"""
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "流水账内容不能为空"}), 400
    category = data.get("category", "工作")
    if category not in ("工作", "会议", "阻塞", "其他"):
        category = "工作"
    now_bj = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO chronicle (content, category, entry_date, created_at) VALUES (?, ?, ?, ?)",
        (content, category, beijing_today(), now_bj),
    )
    conn.commit()
    cid = cursor.lastrowid
    row = dict(cursor.execute("SELECT * FROM chronicle WHERE id = ?", (cid,)).fetchone())
    conn.close()
    return jsonify(row)


@app.route("/api/chronicle/<int:cid>", methods=["PUT"])
def update_chronicle(cid):
    data = request.json or {}
    conn = get_db()
    cursor = conn.cursor()
    existing = cursor.execute("SELECT * FROM chronicle WHERE id = ?", (cid,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "记录不存在"}), 404
    content = (data.get("content") or "").strip() or existing["content"]
    category = data.get("category", existing["category"])
    if category not in ("工作", "会议", "阻塞", "其他"):
        category = existing["category"]
    cursor.execute("UPDATE chronicle SET content=?, category=? WHERE id=?", (content, category, cid))
    conn.commit()
    row = dict(cursor.execute("SELECT * FROM chronicle WHERE id = ?", (cid,)).fetchone())
    conn.close()
    return jsonify(row)


@app.route("/api/chronicle/<int:cid>", methods=["DELETE"])
def delete_chronicle(cid):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chronicle WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# ==================== 报表聚合 API ====================
@app.route("/api/report/overview", methods=["GET"])
def report_overview():
    """任务概览：总数 / 待办 / 进行中 / 已完成 / 逾期。"""
    conn = get_db()
    cursor = conn.cursor()
    total = cursor.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    todo = cursor.execute("SELECT COUNT(*) FROM tasks WHERE status='todo'").fetchone()[0]
    in_progress = cursor.execute("SELECT COUNT(*) FROM tasks WHERE status='in_progress'").fetchone()[0]
    done = cursor.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    today = beijing_today()
    overdue = cursor.execute(
        "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND due_date != '' AND due_date < ?",
        (today,),
    ).fetchone()[0]
    conn.close()
    return jsonify({"total": total, "todo": todo, "in_progress": in_progress,
                    "done": done, "overdue": overdue})


@app.route("/api/report/by-project", methods=["GET"])
def report_by_project():
    """按项目(客户)分布与完成率。"""
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT project, COUNT(*) AS total, "
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
        "FROM tasks GROUP BY project ORDER BY total DESC"
    ).fetchall()
    result = []
    for r in rows:
        proj = r["project"] or "(未归类)"
        total = r["total"]
        done = r["done"]
        rate = round(done / total * 100) if total else 0
        result.append({"project": proj, "total": total, "done": done, "rate": rate})
    conn.close()
    return jsonify(result)


@app.route("/api/report/by-status", methods=["GET"])
def report_by_status():
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status").fetchall()
    conn.close()
    return jsonify([{"status": r["status"], "count": r["c"]} for r in rows])


@app.route("/api/report/by-priority", methods=["GET"])
def report_by_priority():
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT priority, COUNT(*) AS c FROM tasks GROUP BY priority").fetchall()
    conn.close()
    return jsonify([{"priority": r["priority"], "count": r["c"]} for r in rows])


def _ledger_between(cursor, start, end):
    """按日期聚合真实工作动态：进展记录 / 新建任务 / 完成任务。

    返回 {date: {"progress": [...], "created": [...], "done": [...]}}，含 start/end 边界过滤（可均为空=全部）。
    """
    ledger = {}

    def bucket(d):
        if not d:
            return None
        if start and d < start:
            return None
        if end and d > end:
            return None
        if d not in ledger:
            ledger[d] = {"progress": [], "created": [], "done": []}
        return ledger[d]

    for r in cursor.execute(
        "SELECT p.content, p.created_at, t.title AS task_title, t.project "
        "FROM task_progress p LEFT JOIN tasks t ON t.id = p.task_id "
        "ORDER BY p.created_at ASC, p.id ASC"
    ).fetchall():
        d = (r["created_at"] or "")[:10]
        b = bucket(d)
        if b:
            b["progress"].append({
                "time": (r["created_at"] or "")[11:16],
                "task_title": r["task_title"] or "",
                "project": r["project"] or "",
                "content": r["content"],
            })
    for r in cursor.execute(
        "SELECT title, project, created_date FROM tasks "
        "WHERE created_date IS NOT NULL AND created_date != '' ORDER BY id ASC"
    ).fetchall():
        b = bucket(r["created_date"])
        if b:
            b["created"].append({"title": r["title"], "project": r["project"] or ""})
    for r in cursor.execute(
        "SELECT title, project, completion_note, completed_date FROM tasks "
        "WHERE status='done' AND completed_date IS NOT NULL AND completed_date != '' ORDER BY id ASC"
    ).fetchall():
        b = bucket(r["completed_date"])
        if b:
            b["done"].append({"title": r["title"], "project": r["project"] or "",
                              "note": r["completion_note"] or ""})
    return ledger


@app.route("/api/report/export", methods=["GET"])
def report_export():
    """述职导出：按季度(period=2026-Q3)或年度(year=2026)聚合流水账与任务活动，输出 Markdown。"""
    period = (request.args.get("period") or "").strip()
    year = (request.args.get("year") or "").strip()
    if period and re.match(r"^\d{4}-Q[1-4]$", period):
        start, end, label = quarter_start(period), quarter_end(period), f"{period} 季度"
    elif year and re.match(r"^\d{4}$", year):
        start, end, label = f"{year}-01-01", f"{year}-12-31", f"{year} 年度"
    else:
        bn = beijing_now()
        q = (bn.month - 1) // 3 + 1
        period = f"{bn.year}-Q{q}"
        start, end, label = quarter_start(period), quarter_end(period), f"{period} 季度"

    conn = get_db()
    cursor = conn.cursor()
    lines = []
    lines.append(f"# 工作述职报表（{label}）")
    lines.append("")
    lines.append(f"> 生成时间：{beijing_today()}　统计范围：{start} ~ {end}")
    lines.append("")

    total = cursor.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    done = cursor.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    rate = round(done / total * 100) if total else 0
    lines.append("## 一、任务总览")
    lines.append(f"- 任务总数：**{total}**　已完成：{done}　整体完成率：**{rate}%**")
    lines.append("")

    lines.append("## 二、各客户 / 项目完成情况")
    for r in cursor.execute(
        "SELECT project, COUNT(*) t, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) d "
        "FROM tasks GROUP BY project ORDER BY t DESC"
    ).fetchall():
        proj = r["project"] or "(未归类)"
        rt = round(r["d"] / r["t"] * 100) if r["t"] else 0
        lines.append(f"- {proj}：{r['t']} 项，完成 {r['d']} 项（{rt}%）")
    lines.append("")

    lines.append("## 三、工作流水账")
    ledger = _ledger_between(cursor, start, end)
    if ledger:
        for d in sorted(ledger.keys()):
            b = ledger[d]
            lines.append(f"### {d}")
            for it in b["progress"]:
                t = f"〔{it['project']}〕{it['task_title']}" if it["task_title"] else ""
                lines.append(f"- 📝 {t}：{it['content']}")
            for it in b["created"]:
                proj = f"〔{it['project']}〕" if it["project"] else ""
                lines.append(f"- 🆕 {proj}{it['title']}")
            for it in b["done"]:
                proj = f"〔{it['project']}〕" if it["project"] else ""
                note = f" —— {it['note']}" if it["note"] else ""
                lines.append(f"- ✅ {proj}{it['title']}{note}")
    else:
        lines.append("_（本周期暂无工作动态）_")
    lines.append("")

    lines.append("## 四、本周期完成任务")
    cursor.execute(
        "SELECT title, project, completion_note FROM tasks "
        "WHERE status='done' AND completed_date >= ? AND completed_date <= ? ORDER BY completed_date ASC",
        (start, end),
    )
    dones = cursor.fetchall()
    if dones:
        for r in dones:
            proj = f" 〔{r['project']}〕" if r["project"] else ""
            note = f" —— {r['completion_note']}" if r["completion_note"] else ""
            lines.append(f"- {r['title']}{proj}{note}")
    else:
        lines.append("_（本周期暂无完成任务）_")
    lines.append("")

    conn.close()
    md = "\n".join(lines)
    from urllib.parse import quote
    fname = f"述职报表_{label}.md"
    disposition = f"attachment; filename=\"report.md\"; filename*=UTF-8''{quote(fname)}"
    return Response(md, mimetype="text/markdown",
                    headers={"Content-Disposition": disposition})


# ==================== 每日任务动态汇总（日志自动骨架） ====================
@app.route("/api/daily-summary", methods=["GET"])
def get_daily_summary():
    """按日期汇总任务动态，作为每日日志的自动骨架（看板为单一事实源）"""
    d = request.args.get("date", date.today().isoformat())
    conn = get_db()
    cursor = conn.cursor()
    cols = "id,title,project,priority,status,completion_note"
    # 今日新建（今天创建且尚未完成）
    cursor.execute(f"SELECT {cols} FROM tasks WHERE created_date = ? AND status != 'done' ORDER BY sort_order ASC, id DESC", (d,))
    created = [dict(r) for r in cursor.fetchall()]
    # 今日完成
    cursor.execute(f"SELECT {cols} FROM tasks WHERE completed_date = ? AND status='done' ORDER BY id DESC", (d,))
    done = [dict(r) for r in cursor.fetchall()]
    # 进行中（历史创建、未完成、非今日新建）
    cursor.execute(f"SELECT {cols} FROM tasks WHERE created_date < ? AND status != 'done' ORDER BY sort_order ASC, {PRIORITY_ORDER_SQL} ASC, id DESC", (d,))
    ongoing = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"date": d, "created": created, "ongoing": ongoing, "done": done})

# ==================== 工作流水账（Markdown 导出） ====================
@app.route("/api/daily-ledger", methods=["GET"])
def daily_ledger_api():
    """按日期倒序返回近 N 天真实工作动态（进展记录/新建/完成任务），供报表中心预览。"""
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    start = (beijing_now().date() - timedelta(days=days - 1)).isoformat()
    conn = get_db()
    cursor = conn.cursor()
    ledger = _ledger_between(cursor, start, None)
    conn.close()
    return jsonify([{"date": d, **ledger[d]} for d in sorted(ledger.keys(), reverse=True)])

# ==================== 数据导出 ====================

@app.route("/api/export", methods=["GET"])
def export_data():
    """导出所有数据为 JSON"""
    export_type = request.args.get("type", "all")
    conn = get_db()
    cursor = conn.cursor()
    
    data = {
        "export_date": beijing_today(),
        "version": "1.0"
    }
    
    if export_type in ("all", "tasks"):
        cursor.execute("SELECT * FROM tasks ORDER BY created_date DESC")
        data["tasks"] = [dict(row) for row in cursor.fetchall()]
    
    if export_type in ("all", "project_links"):
        cursor.execute("SELECT * FROM project_links ORDER BY project_name")
        data["project_links"] = [dict(row) for row in cursor.fetchall()]

    if export_type in ("all", "daily_logs"):
        cursor.execute("SELECT * FROM daily_logs ORDER BY log_date DESC, id DESC")
        data["daily_logs"] = [dict(row) for row in cursor.fetchall()]

    if export_type in ("all", "notes"):
        cursor.execute("SELECT * FROM notes ORDER BY sort_order ASC, id DESC")
        data["notes"] = [dict(row) for row in cursor.fetchall()]

    if export_type in ("all", "task_progress"):
        cursor.execute("SELECT * FROM task_progress ORDER BY id DESC")
        data["task_progress"] = [dict(row) for row in cursor.fetchall()]

    if export_type in ("all", "chronicle"):
        cursor.execute("SELECT * FROM chronicle ORDER BY id DESC")
        data["chronicle"] = [dict(row) for row in cursor.fetchall()]

    conn.close()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"workbench_export_{timestamp}.json"
    filepath = os.path.join(EXPORTS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    return jsonify({
        "success": True,
        "filename": filename,
        "filepath": filepath,
        "data": data
    })

# ==================== 手动一键备份 ====================
@app.route("/api/backup-now", methods=["POST"])
def backup_now():
    """立即把当前数据库快照备份到 backup 目录（时间戳命名，不覆盖每日自动备份）。"""
    ts = beijing_now().strftime("%Y%m%d_%H%M%S")
    fname = f"workbench_manual_{ts}.db"
    dest = os.path.join(BACKUP_DIR, fname)
    try:
        shutil.copy2(DB_PATH, dest)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "filename": fname, "path": dest})


# ==================== 退出接口（网页「退出程序」按钮调用） ====================
_server = None

@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """由网页端「退出程序」按钮调用，优雅关闭本地服务。"""
    def _do_shutdown():
        import time
        time.sleep(0.3)
        try:
            if _server is not None:
                _server.shutdown()
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return jsonify({"success": True, "msg": "正在退出…"})


# ==================== 启动 ====================

if __name__ == "__main__":
    print("=" * 55)
    print("  📋 个人工作台 v1.0")
    print(f"  📂 数据库: {DB_PATH}")
    print(f"  💾 备份目录: {BACKUP_DIR}")
    print(f"  📤 导出目录: {EXPORTS_DIR}")
    PORT = int(os.environ.get("PORT", 5000))
    print(f"  🌐 访问地址: http://127.0.0.1:{PORT}")
    print("  🔘  关闭：在页面内点「退出程序」按钮（或 Ctrl+C）")
    print("=" * 55)
    # 单实例检测与服务启动见下方

    import socket
    import webbrowser
    from werkzeug.serving import make_server

    # 单实例：端口已被占用（多半是上次没退出的实例），直接打开已有页面并退出，
    # 避免重复启动占满端口导致卡死
    def _port_in_use(port, host="127.0.0.1"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex((host, port)) == 0

    if _port_in_use(PORT):
        print(f"  ⚠️ 端口 {PORT} 已被占用，视为已有实例在运行，打开浏览器并退出。")
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}")
        except Exception:
            pass
        sys.exit(0)

    if IS_FROZEN:
        threading.Timer(3.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()

    _server = make_server("127.0.0.1", PORT, app)
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已通过 Ctrl+C 退出")
    finally:
        _server.server_close()
