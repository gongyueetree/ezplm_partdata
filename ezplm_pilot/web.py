"""ezPLM 暂存数据平台 · 管理后台（FastAPI + Jinja2，单文件，无前端构建）。

启动：python -m ezplm_pilot.cli serve --port 8080   或   uvicorn ezplm_pilot.web:app
登录：设置环境变量 ADMIN_PASSWORD（HTTP Basic，用户名任意）；未设置则不鉴权（仅限本机调试）
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import DictLoader, Environment
from sqlalchemy import func, or_, select

from . import pipeline, settings
from .jobs import manager
from .staging import CategoryParam, StagedCategory, StagedPart, StagedProposal
from .staging import get_session as _new_session
import contextvars

_REQ_DB: contextvars.ContextVar = contextvars.ContextVar("req_db", default=None)


def get_session():
    """请求级会话：同一请求内复用，请求结束由中间件统一关闭（否则 Postgres 连接池会被耗尽）。"""
    holder = _REQ_DB.get()
    if holder is None:                      # 非请求上下文（启动钩子、后台线程）
        return _new_session()
    if not holder:
        holder.append(_new_session())
    return holder[0]

app = FastAPI(title="ezPLM 暂存数据平台")
security = HTTPBasic(auto_error=False)


@app.middleware("http")
async def _db_session_scope(request: Request, call_next):
    holder: list = []
    token = _REQ_DB.set(holder)
    try:
        return await call_next(request)
    finally:
        _REQ_DB.reset(token)
        for db in holder:
            try:
                db.close()
            except Exception:  # noqa
                pass


@app.exception_handler(Exception)
async def _show_error(request: Request, exc: Exception):
    """内部后台：把异常堆栈直接显示出来，便于排错（而不是只给一句 Internal Server Error）。"""
    import traceback
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(tb)
    return HTMLResponse(f"<h2 style='font-family:sans-serif'>页面出错（请把下面内容截图发给开发者）</h2><pre style='white-space:pre-wrap;font-size:12px'>{tb.replace('<','&lt;')}</pre>", status_code=500)


def auth(cred: HTTPBasicCredentials | None = Depends(security)):
    pw = os.environ.get("ADMIN_PASSWORD") or os.environ.get("EZPLM_WEB_PASSWORD")
    if not pw:
        return "admin"
    if cred and secrets.compare_digest(cred.password, pw):
        return cred.username or "admin"
    raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


# =========================================================================== 模板
BASE = """<!doctype html><html lang=zh><head><meta charset=utf-8><title>{{ title }} · 暂存数据平台</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#1f2328;--mute:#6b7280;--line:#e5e7eb;--acc:#2563eb;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--side:#ffffff}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);display:flex;min-height:100vh}
aside{width:210px;background:var(--side);border-right:1px solid var(--line);padding:16px 12px;flex-shrink:0;position:sticky;top:0;height:100vh}
aside .brand{font-weight:700;font-size:15px;padding:6px 10px 14px;display:flex;align-items:center;gap:8px}aside .brand i{display:inline-block;width:26px;height:26px;border-radius:7px;background:var(--acc)}
aside .grp{font-size:11px;color:var(--mute);padding:10px 10px 4px}aside a{display:block;padding:7px 10px;border-radius:7px;color:var(--ink);text-decoration:none;font-size:13.5px}
aside a.on{background:#eef2ff;color:#1e3a8a;font-weight:600}aside a:hover{background:#f3f4f6}
main{flex:1;padding:20px 26px;max-width:1500px;min-width:0}
.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}.top h1{font-size:19px;margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}.card .n{font-size:23px;font-weight:600}.card .l{color:var(--mute);font-size:12px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}.panel h2{font-size:14.5px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#f3f4f6;font-weight:600;white-space:nowrap}
tr:hover td{background:#fafafa}.mute{color:var(--mute)}.tag{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:4px;padding:0 6px;font-size:11px;margin:1px 2px 1px 0}
.act-auto_fill{color:var(--ok)}.act-flag_mismatch{color:var(--bad)}.act-review{color:var(--warn)}
.bar{height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
form.inline{display:inline}button,.btn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:5px 11px;cursor:pointer;font-size:12.5px;text-decoration:none;color:var(--ink)}
button.ok{background:var(--ok);color:#fff;border-color:var(--ok)}button.bad{background:#fff;color:var(--bad);border-color:var(--bad)}button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}input,select,textarea{padding:6px 8px;border:1px solid var(--line);border-radius:6px;font-size:13px;font-family:inherit}
.pager{margin:12px 0;display:flex;gap:10px;align-items:center}.pager a{color:var(--acc)}
pre{white-space:pre-wrap;word-break:break-all;background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;font-size:12px;max-height:420px;overflow:auto;font-family:ui-monospace,Menlo,monospace}
.new{background:#ecfdf5}.diff{color:var(--bad);font-weight:600}h2{font-size:15px;margin:22px 0 8px}
.kv td:first-child{width:130px;color:var(--mute)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:1000px){.grid2{grid-template-columns:1fr}}
.frm label{display:block;font-size:12px;color:var(--mute);margin:8px 0 3px}.frm .row{display:flex;gap:10px;flex-wrap:wrap}.frm .row>div{flex:1;min-width:120px}
.status-running{color:var(--acc)}.status-done{color:var(--ok)}.status-failed{color:var(--bad)}.status-stopped{color:var(--warn)}
.msg{background:#ecfdf5;border:1px solid #a7f3d0;padding:8px 12px;border-radius:8px;margin-bottom:12px}.err{background:#fef2f2;border-color:#fecaca}
.tree li{list-style:none;padding:2px 0}.tree ul{padding-left:18px;margin:0}.tree .cnt{color:var(--mute);font-size:12px}
</style></head><body>
<aside><div class=brand><i></i>暂存数据平台</div>
<div class=grp>概览</div><a href="/" class="{{ 'on' if nav=='home' }}">工作台</a><a href="/jobs" class="{{ 'on' if nav=='jobs' }}">任务控制</a>
<div class=grp>数据</div><a href="/parts" class="{{ 'on' if nav=='parts' }}">物料信息</a><a href="/categories" class="{{ 'on' if nav=='categories' }}">分类</a><a href="/library" class="{{ 'on' if nav=='library' }}">符号 / 封装库</a>
<div class=grp>治理</div><a href="/review" class="{{ 'on' if nav=='review' }}">复核队列</a><a href="/export" class="{{ 'on' if nav=='export' }}">回写 ezPLM</a>
<div class=grp>系统</div><a href="/settings" class="{{ 'on' if nav=='settings' }}">设置</a>
<div style="margin-top:20px;padding:0 10px" class=mute>{{ user }}{% if busy %}<br><span class=status-running>● 任务运行中</span>{% endif %}</div></aside>
<main><div class=top><h1>{{ title }}</h1>{% block topright %}{% endblock %}</div>
{% if msg %}<div class="msg {{ 'err' if err }}">{{ msg }}</div>{% endif %}
{% block body %}{% endblock %}</main>
{% block script %}{% endblock %}</body></html>"""

HOME = """{% extends "base" %}{% block body %}
<div class=cards>
<div class=card><div class=n>{{ s.parts_total }}</div><div class=l>暂存物料（ezPLM 翻到第 {{ s.ingest_page }} 页）</div></div>
<div class=card><div class=n>{{ s.parts_processed }}</div><div class=l>已处理</div><div class=bar><i style="width:{{ pct(s.parts_processed, s.parts_total) }}%"></i></div></div>
<div class=card><div class=n>{{ s.proposals.get('pending',0) }}</div><div class=l>待人工复核</div></div>
<div class=card><div class=n>{{ s.proposals.get('auto',0) }}</div><div class=l>已自动写入（可撤销）</div></div>
<div class=card><div class=n>{{ s.proposals.get('approved',0) }} / {{ s.proposals.get('rejected',0) }}</div><div class=l>复核通过 / 驳回</div></div>
<div class=card><div class=n>{{ s.parts_exported }}</div><div class=l>已回写 ezPLM</div></div>
</div>
<div class=grid2>
<div class=panel><h2>字段缺口与补全</h2>
<table><tr><th>字段</th><th>原缺失</th><th>已补全</th><th>进度</th></tr>
{% for f,label in [('category','分类'),('symbol','符号'),('footprint','封装'),('url','官网链接'),('pdf','数据手册')] %}
{% set filled = s.filled.get('datasheet' if f=='pdf' else f, 0) %}
<tr><td>{{ label }}</td><td>{{ s.missing.get(f, '-') }}</td><td>{{ filled }}</td><td style="width:35%"><div class=bar><i style="width:{{ pct(filled, s.missing.get(f,0)) }}%"></i></div></td></tr>
{% endfor %}</table></div>
<div class=panel><h2>当前任务</h2>
{% if job %}<div>{{ job.kind }} <span class="status-{{ job.status }}">{{ job.status }}</span> · {{ job.progress.done }}{% if job.progress.total %} / {{ job.progress.total }}{% endif %} · 开始 {{ job.started_at }}</div>
<pre style="max-height:180px">{{ job.log[-8:]|join('\\n') }}</pre><a class=btn href="/jobs">去任务控制 →</a>
{% else %}<p class=mute>没有运行中的任务。</p><a class=btn href="/jobs">启动任务 →</a>{% endif %}</div>
</div>
<div class=panel><h2>建议分布 <span class=mute style="font-weight:400">当前有效（待复核 + 自动写入）</span></h2>
<table><tr><th>字段</th><th>动作</th><th>条数</th></tr>{% for f,a,n in s.by_field_action %}<tr><td>{{ f }}</td><td class="act-{{ a }}">{{ a }}</td><td>{{ n }}</td></tr>{% endfor %}</table></div>
<div class=panel><h2>最近批次</h2>
<table><tr><th>任务</th><th>阶段</th><th>批次</th><th>条数</th><th>时间</th></tr>{% for j in s.jobs %}<tr><td>{{ j.job }}</td><td>{{ j.stage }}</td><td>{{ j.batch_no }}</td><td>{{ j.n }}</td><td class=mute>{{ j.at }}</td></tr>{% endfor %}</table></div>
{% endblock %}"""

JOBS = """{% extends "base" %}{% block body %}
<div class=grid2>
<div>
<div class=panel><h2>① 从 ezPLM 拉取（ingest）</h2>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=ingest>
<div class=row><div><label>数据源</label><select name=source><option value=api>ezPLM API（设置里配置）</option><option value=sample>内置样本（演示）</option></select></div>
<div><label>每批条数</label><input name=batch value="{{ cfg.ingest_batch }}"></div><div><label>最多批次（空=全部）</label><input name=max_batches placeholder="如 20"></div></div>
<label><input type=checkbox name=reset value=1> 从第 1 页重新开始（否则从上次页码续）</label>
<label><input type=checkbox name=incremental value=1> <b>只拉新增</b>：从第 1 页开始，遇到整批都已存在即停（全量拉完后每天点一次，同步同事新上传的器件）</label>
<p><button class=pri {{ 'disabled' if busy }}>开始拉取</button> <span class=mute>当前续跑页码：{{ s.ingest_page }}</span></p></form></div>

<div class=panel><h2>② 处理（process）</h2>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=process>
<div class=row><div><label>每批条数</label><input name=batch value="{{ cfg.process_batch }}"></div><div><label>最多批次（空=全部）</label><input name=max_batches placeholder="如 10"></div></div>
<label><input type=checkbox name=llm value=1> 启用 LLM 仲裁（需在设置里填 API Key）</label>
<label><input type=checkbox name=urls value=1> 补全官网/数据手册链接（联网验证，较慢）</label>
<label><input type=checkbox name=details value=1 checked> 拉取物料属性（缺封装的物料逐条调 ezPLM 详情接口，用于封装映射；较慢，首轮全量可先不勾）</label>
<p><button class=pri {{ 'disabled' if busy }}>开始处理</button> <span class=mute>待处理 {{ s.parts_total - s.parts_processed }} 条</span></p></form>
<form method=post action="/jobs/reset-processed" onsubmit="return confirm('把所有物料标记为未处理？这些物料的待复核建议会作废（重新处理时重新生成）；已写入的新值保留。')"><button>重置处理状态（规则更新后重跑）</button></form></div>

<div class=panel><h2>③ 扫描符号/封装来源（scan）</h2>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=scan>
<label>Git 仓库地址（或服务器上的目录路径）</label><input name=path style="width:100%" placeholder="https://github.com/espressif/kicad-libraries.git">
<div class=row><div><label>来源可信级别</label><select name=source_kind>{% for k in ['kicad-official','vendor-official','lcsc-easyeda','digikey-library','oshw-product','community'] %}<option>{{ k }}</option>{% endfor %}</select></div></div>
<p><button class=pri {{ 'disabled' if busy }}>克隆并扫描</button> <span class=mute>索引：符号 {{ lib.symbols }} / 封装 {{ lib.footprints }}</span></p></form>
<div class=mute>已下载的来源：{{ sources|join('、') or '无' }}</div></div>

<div class=panel><h2>IHS 参考数据</h2>
<p class=mute>IHS 参数库（2012 年快照）里与暂存物料同型号的记录：{{ ihs_n }} 条；已匹配物料 {{ s.ihs_matched }} 条。原始数据约 7000 万条，不放服务器：在本地运行
<code>python -m ezplm_pilot.cli ihs-load --dir &lt;IHS数据目录&gt;</code>（<code>DATABASE_URL</code> 指向本平台的 Postgres），ezPLM 陆续拉取后再运行一次即可。处理（②）会自动带上 IHS 建议；已处理过的物料用下面的按钮补跑。</p>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=ihs>
<label><input type=checkbox name=all value=1> 也检查 IHS 无记录的物料的厂商名（提示已被并购的旧厂商名）</label>
<p><button class=pri {{ 'disabled' if busy or not ihs_n }}>补跑 IHS 匹配</button></p></form></div>

<div class=panel><h2>ezPLM 基础数据（厂商 / 符号 / 封装）</h2>
<p class=mute>以 ezPLM 自己的库为准：厂商名和别名作为厂商标准；符号/封装建议标出 ezPLM ID，ezPLM 没有的打"需新建"；以型号命名的 ezPLM 符号直接按型号匹配。
当前：厂商 {{ ezc.companies }} / 符号 {{ ezc.symbols }} / 封装 {{ ezc.footprints }}。只读拉取，整表重拉约几分钟。</p>
<form class=inline method=post action="/jobs/start"><input type=hidden name=kind value=ezplm-lib><button class=pri {{ 'disabled' if busy }}>从 ezPLM 拉取厂商、符号、封装清单</button></form></div>

<div class=panel><h2>IHS 参数（试点）</h2>
<p class=mute>按分类让模型生成"IHS 参数列 → 关键参数"映射（每个分类调用一次），到 分类 → IHS 映射 页人工确认后，再写参数。
映射：草稿 {{ imap.get('draft', 0) }} / 已确认 {{ imap.get('approved', 0) }}；已写参数 {{ n_pparams }} 个。</p>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=ihs-maps>
<div class=row><div><label>分类路径（正则）</label><input name=path_re value="放大器|稳压器|电压基准|比较器|微控制器" style="width:100%"></div>
<div><label>至少多少条可靠 IHS 匹配</label><input name=min_parts value="20"></div></div>
<p><button class=pri {{ 'disabled' if busy }}>为这些分类生成映射草稿</button> <span class=mute>已有映射的分类跳过</span></p></form>
<form class=inline method=post action="/jobs/start"><input type=hidden name=kind value=ihs-params><button {{ 'disabled' if busy or not imap.get('approved') }}>按全部已确认映射写参数</button></form></div>

<div class=panel><h2>④ 回写 ezPLM（export）</h2>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=export>
<p><button {{ 'disabled' if busy }}>生成回写包（不写 ezPLM）</button> <a class=btn href="/export">查看 / 下载 / 正式回写 →</a></p></form></div>
</div>

<div>
<div class=panel><h2>当前任务 {% if busy %}<form class=inline method=post action="/jobs/stop"><button class=bad>停止</button></form>{% endif %}</h2>
<div id=jobbox>{% if job %}<div><b>{{ job.id }}</b> <span class="status-{{ job.status }}">{{ status_zh.get(job.status, job.status) }}</span>{% if job.status in ('running','queued') or job.progress.total %} · 进度 {{ job.progress.done }}{% if job.progress.total %} / {{ job.progress.total }}{% endif %}{% endif %}{% if job.finished_at %} · 结束于 {{ job.finished_at }}{% endif %}</div>
<pre id=joblog>{{ job.log|join('\\n') }}</pre>{% else %}<p class=mute>无</p>{% endif %}</div></div>
<div class=panel><h2>历史任务</h2>
<table><tr><th>任务</th><th>状态</th><th>开始</th><th>结束</th><th>结果</th></tr>
{% for h in history %}<tr><td>{{ h.id }}</td><td class="status-{{ h.status }}">{{ status_zh.get(h.status, h.status) }}</td><td class=mute>{{ h.started_at }}</td><td class=mute>{{ h.finished_at }}</td><td class=mute style="max-width:300px">{{ h.result }}</td></tr>{% endfor %}</table></div>
</div></div>
{% endblock %}{% block script %}{% if busy %}<script>
const ZH={queued:'排队中',running:'运行中',done:'已完成',failed:'失败',stopped:'已停止'};
setInterval(async()=>{const r=await fetch('/api/jobs/current');const j=await r.json();if(!j){location.replace(location.pathname);return}
const fin=j.status!=='running'&&j.status!=='queued';
document.getElementById('jobbox').innerHTML='<div><b>'+j.id+'</b> <span class="status-'+j.status+'">'+(ZH[j.status]||j.status)+'</span>'+(fin&&!j.progress.total?'':' · 进度 '+j.progress.done+(j.progress.total?' / '+j.progress.total:''))+'</div><pre id=joblog>'+j.log.join('\\n').replace(/</g,'&lt;')+'</pre>';
const p=document.getElementById('joblog');p.scrollTop=p.scrollHeight;if(fin){setTimeout(()=>location.replace(location.pathname),1500)}},2000);
</script>{% endif %}{% endblock %}"""

PARTS = """{% extends "base" %}{% block body %}
<form class=filters method=get>
<input name=q value="{{ q }}" placeholder="型号 / 制造商 / 描述" style="width:240px">
<select name=missing><option value="">— 缺失字段 —</option>{% for v,l in [('category','分类'),('symbol','符号'),('footprint','封装'),('url','官网链接'),('pdf','PDF')] %}<option value="{{ v }}" {{ 'selected' if missing==v }}>{{ l }}</option>{% endfor %}</select>
<select name=changed><option value="">— 是否有新值 —</option><option value=1 {{ 'selected' if changed=='1' }}>有新值</option><option value=0 {{ 'selected' if changed=='0' }}>无新值</option></select>
<select name=processed><option value="">— 处理状态 —</option><option value=1 {{ 'selected' if processed=='1' }}>已处理</option><option value=0 {{ 'selected' if processed=='0' }}>未处理</option></select>
<input name=tag value="{{ tag }}" placeholder="标签" style="width:140px"><button class=pri>筛选</button><span class=mute>共 {{ total }} 条</span></form>
<table><tr><th>型号</th><th>制造商</th><th>描述</th><th>分类（原 → 新）</th><th>待复核建议</th><th>符号</th><th>封装</th><th>PDF</th><th>标签</th><th>分</th></tr>
{% for p in rows %}<tr>
<td><a href="/parts/{{ p.id }}">{{ p.mpn }}</a></td><td class=mute>{{ p.manufacturer }}</td><td class=mute style="max-width:280px">{{ (p.description or '')[:90] }}</td>
<td style="max-width:220px">{{ paths.get(p.orig_category_id) or '<span class=mute>空</span>'|safe }}{% if p.new_category_id %}<br>→ <span class=diff>{{ paths.get(p.new_category_id, p.new_category) }}</span>{% endif %}</td>
<td style="max-width:220px">{% for s in sugg.get(p.id, []) %}<div><span class=mute>{{ s.field }}</span> {{ s.proposed_label or s.proposed }} <span class=mute>{{ '%.2f'|format(s.confidence or 0) }}</span></div>{% endfor %}</td>
<td>{{ p.orig_symbol or '' }}{% if p.new_symbol %}<span class=diff> → {{ p.new_symbol }}</span>{% endif %}</td>
<td>{{ p.orig_footprint or '' }}{% if p.new_footprint %}<span class=diff> → {{ p.new_footprint }}</span>{% endif %}</td>
<td>{{ '有' if p.orig_has_pdf else '无' }}</td><td>{% for t in (p.tags or '').split(',') if t %}<span class=tag>{{ t }}</span>{% endfor %}</td><td>{{ p.value_score }}</td>
</tr>{% endfor %}</table>
<div class=pager>{% if page>1 %}<a href="?{{ qs }}&page={{ page-1 }}">上一页</a>{% endif %}<span>第 {{ page }} / {{ ((total-1)//size)+1 if total else 1 }} 页</span>{% if page*size<total %}<a href="?{{ qs }}&page={{ page+1 }}">下一页</a>{% endif %}</div>
{% endblock %}"""

PART = """{% extends "base" %}{% block topright %}<span class=mute>{{ p.manufacturer }} · {{ p.id }}</span>{% endblock %}{% block body %}
<div class=grid2>
<div class=panel><h2>字段（原值 → 新值）</h2><table class=kv>
<tr><td>描述</td><td>{{ p.description }}<br><span class=mute>{{ p.description_en }}</span>{% if p.new_description_en %}<br>→ <span class=diff>{{ p.new_description_en }}</span>{% endif %}</td></tr>
<tr><td>制造商</td><td>{{ p.manufacturer or '空' }}{% if p.new_manufacturer %} → <span class=diff>{{ p.new_manufacturer }}</span>{% endif %}{% if ihs.mine and ihs.mine.conf %}<br><span class=mute>现属 {{ ihs.mine.current }}（谱系 {{ ihs.mine.conf }}：{{ ihs.mine.note }}）</span>{% endif %}</td></tr>
<tr><td>分类</td><td>{{ tax.path(p.orig_category_id) if p.orig_category_id in tax.nodes else (p.orig_category or '空') }}{% if p.new_category_id %}<br>→ <span class=diff>{{ tax.path(p.new_category_id) }}</span>{% endif %}</td></tr>
<tr><td>符号</td><td>{{ p.orig_symbol or '空' }} {{ ('→ <span class=diff>'+p.new_symbol+'</span>')|safe if p.new_symbol }}</td></tr>
<tr><td>封装</td><td>{{ p.orig_footprint or '空' }} {{ ('→ <span class=diff>'+p.new_footprint+'</span>')|safe if p.new_footprint }}</td></tr>
<tr><td>官网链接</td><td>{{ p.orig_url or '空' }} {% if p.new_url %}→ <a class=diff href="{{ p.new_url }}" target=_blank>{{ p.new_url }}</a>{% endif %}</td></tr>
<tr><td>数据手册</td><td>{{ '有 PDF' if p.orig_has_pdf else '无' }} {% if p.new_datasheet %}→ <a class=diff href="{{ p.new_datasheet }}" target=_blank>{{ p.new_datasheet }}</a>{% endif %}</td></tr>
<tr><td>标签</td><td>{% for t in (p.tags or '').split(',') if t %}<span class=tag>{{ t }}</span>{% endfor %}</td></tr>
<tr><td>状态</td><td>处理 {{ p.processed_at or '未' }} · 回写 {{ p.exported_at or '未' }} · 价值分 {{ p.value_score }}</td></tr>
</table></div>
<div class=panel><h2>手工修改新值</h2>
<form class=frm method=post action="/parts/{{ p.id }}/edit">
<label>分类（留空=不改；输入"-"=清除新值）</label><input list=cats name=new_category_path style="width:100%" placeholder="输入关键字选择，如 数字隔离器">
<datalist id=cats>{% for cid, path in leaves %}<option value="{{ path }}">{% endfor %}</datalist>
<div class=row><div><label>符号（KiCad 名）</label><input name=new_symbol value="{{ p.new_symbol or '' }}"></div><div><label>封装（KiCad 名）</label><input name=new_footprint value="{{ p.new_footprint or '' }}"></div></div>
<div class=row><div><label>官网链接</label><input name=new_url value="{{ p.new_url or '' }}"></div><div><label>数据手册 URL</label><input name=new_datasheet value="{{ p.new_datasheet or '' }}"></div></div>
<div class=row><div><label>英文描述</label><input name=new_description_en value="{{ p.new_description_en or '' }}"></div><div><label>制造商（只填空，不覆盖 ezPLM 已有值）</label><input name=new_manufacturer value="{{ p.new_manufacturer or '' }}"></div></div>
<label>标签（逗号分隔）</label><input name=tags value="{{ p.tags or '' }}" style="width:100%">
<p><button class=pri>保存</button> <button name=reprocess value=1>保存并标记为待重新处理</button></p></form></div></div>
{% if pparams %}<div class=panel><h2>参数 <span class=mute style="font-weight:400">来自 IHS 映射（2012 年数据，未回写 ezPLM）</span></h2>
<table><tr><th>参数</th><th>值</th><th>来源</th></tr>{% for pp in pparams %}<tr><td>{{ pnames.get(pp.key, pp.key) }}</td><td><b>{{ pp.value }}</b></td><td class=mute>{{ pp.detail }}</td></tr>{% endfor %}</table></div>{% endif %}
<div class=panel><h2>IHS 参考 <span class=mute style="font-weight:400">2011-12 ~ 2012-02 快照 · 同型号 {{ ihs.total or 0 }} 条</span></h2>
{% if ihs.rows %}<div style="overflow-x:auto"><table><tr><th>匹配</th><th>置信度</th><th>IHS 型号</th><th>IHS 厂商 → 当前归属</th><th>IHS 分类</th><th>描述</th><th>封装</th><th>状态（2012）</th><th>参数</th></tr>
{% for x in ihs.rows %}<tr class="{{ 'new' if x.r.objid == p.ihs_objid }}">
<td style="white-space:nowrap">{{ x.level }}</td><td>{{ '%.2f'|format(x.conf) }}</td>
<td>{{ x.r.mpn }}{% if x.r.generic %}<br><span class=mute>通用号 {{ x.r.generic }}</span>{% endif %}</td>
<td>{{ x.r.mfr_ihs }}{% if x.r.lineage_conf %}<br>→ <b>{{ x.r.mfr_current }}</b> <span class=tag>谱系 {{ x.r.lineage_conf }}</span>{% endif %}</td>
<td>{{ x.r.family }}<br><span class=mute>{{ x.r.part_category }}</span></td><td style="max-width:280px">{{ x.r.description }}</td>
<td class=mute>{{ x.r.package }}</td><td>{{ x.r.status }}</td>
<td><details><summary class=mute>{{ x.params|length }} 项</summary><table class=kv>{% for k, v in x.params.items() %}<tr><td>{{ k }}</td><td>{{ v }}</td></tr>{% endfor %}</table>
{% if x.r.datasheet %}<a href="{{ x.r.datasheet }}" target=_blank rel=noopener>IHS 数据手册链接（多已失效）</a>{% endif %}</details></td></tr>{% endfor %}</table></div>
<p class=mute>绿色行是本物料的最佳匹配，IHS 建议依据它生成。L5（同号异厂）只作参考，不出建议。状态和数据手册链接是 2012 年的数据。{% if ihs.total > ihs.rows|length %}只显示前 {{ ihs.rows|length }} 条。{% endif %}</p>
{% else %}<p class=mute>IHS 中没有同型号记录。{% if not ihs_n %}暂存库里还没有 IHS 参考数据：在本地运行 <code>python -m ezplm_pilot.cli ihs-load --dir &lt;IHS数据目录&gt;</code>。{% endif %}</p>{% endif %}</div>
<div class=panel><h2>建议与复核</h2>
<table><tr><th>字段</th><th>现值</th><th>建议值</th><th>置信度</th><th>动作</th><th>方法</th><th>状态</th><th>操作</th><th>证据</th></tr>
{% for r in props %}<tr class="{{ 'new' if r.review_status in ('auto','approved') }}">
<td>{{ r.field }}</td><td class=mute>{{ r.current or '空' }}</td><td><b>{{ r.proposed_label or r.proposed }}</b></td><td>{{ '%.2f'|format(r.confidence or 0) }}</td>
<td class="act-{{ r.action }}">{{ r.action }}</td><td class=mute>{{ r.method }}</td><td>{{ r.review_status }}{% if r.reviewed_by %}<br><span class=mute>{{ r.reviewed_by }} {{ r.reviewed_at }}</span>{% endif %}</td>
<td style="white-space:nowrap">{% if r.review_status=='pending' %}<form class=inline method=post action="/review/{{ r.id }}/approve"><button class=ok>通过</button></form> <form class=inline method=post action="/review/{{ r.id }}/reject"><button class=bad>驳回</button></form>
{% elif r.review_status=='auto' %}<form class=inline method=post action="/review/{{ r.id }}/revert"><button class=bad>撤销自动写入</button></form>{% endif %}</td>
<td><details><summary class=mute>查看</summary><pre style="background:#f3f4f6;color:#111">{{ r.evidence_json }}</pre></details></td></tr>{% endfor %}</table>
<h2>物料属性 / 备注</h2><pre style="background:#f3f4f6;color:#111">{{ p.attributes_json if p.attributes_json not in ('{}', '', None) else '（无属性；处理时会自动从 ezPLM 详情接口拉取）' }}
{{ p.notes or '' }}</pre>
{% endblock %}"""

REVIEW = """{% extends "base" %}{% block body %}
<form class=filters method=get>
<select name=field><option value="">— 字段 —</option>{% for v in ['category_id','symbol','footprint','url','datasheet','description_en','manufacturer'] %}<option {{ 'selected' if field==v }}>{{ v }}</option>{% endfor %}</select>
<select name=action><option value="">— 动作 —</option>{% for v in ['flag_mismatch','review'] %}<option {{ 'selected' if action==v }}>{{ v }}</option>{% endfor %}</select>
<select name=status><option value=pending {{ 'selected' if status=='pending' }}>待复核</option><option value=auto {{ 'selected' if status=='auto' }}>自动写入（抽检）</option><option value=approved {{ 'selected' if status=='approved' }}>已通过</option><option value=rejected {{ 'selected' if status=='rejected' }}>已驳回</option></select>
<input name=minconf value="{{ minconf }}" placeholder="最低置信度" style="width:110px"><button class=pri>筛选</button><span class=mute>共 {{ total }} 条</span></form>
{# 操作按钮各自一个表单，放在筛选表单外面：表单不能嵌套，嵌套时浏览器会忽略内层表单，点按钮只会提交筛选 #}
{% if status=='pending' %}<div class=filters>
<form class=inline method=post action="/review/dedupe" onsubmit="return confirm('作废重复的待复核建议（同一物料、同一字段、同一建议值只留最新一条）？')"><button>清理重复建议</button></form>
<form class=inline method=post action="/review/void-stale" onsubmit="return confirm('作废“未处理”物料上的待复核建议？这些是旧规则生成的，物料重新处理时会按新规则重新生成。')"><button>作废未处理物料上的旧建议</button></form>
{% if rows %}<form class=inline method=post action="/review/bulk?next={{ next|urlencode }}" onsubmit="return confirm('通过本页全部 {{ rows|length }} 条？')"><input type=hidden name=ids value="{{ rows|map(attribute='0.id')|join(',') }}"><button class=ok>本页全部通过</button></form>{% endif %}
</div>{% endif %}
<table><tr><th>型号</th><th>制造商</th><th>描述</th><th>字段</th><th>现值</th><th>建议值</th><th>置信度</th><th>动作</th><th>方法</th><th>操作</th></tr>
{% for r,p in rows %}<tr>
<td><a href="/parts/{{ p.id }}">{{ p.mpn }}</a></td><td class=mute>{{ p.manufacturer }}</td><td class=mute style="max-width:260px">{{ (p.description or '')[:80] }}</td>
<td>{{ r.field }}</td><td class=mute>{{ r.current_label or r.current or '空' }}</td><td><b>{{ r.proposed_label or r.proposed }}</b></td><td>{{ '%.2f'|format(r.confidence or 0) }}</td>
<td class="act-{{ r.action }}">{{ r.action }}</td><td class=mute>{{ r.method }}</td>
<td style="white-space:nowrap">{% if r.review_status=='pending' %}<form class=inline method=post action="/review/{{ r.id }}/approve?next={{ next|urlencode }}"><button class=ok>通过</button></form> <form class=inline method=post action="/review/{{ r.id }}/reject?next={{ next|urlencode }}"><button class=bad>驳回</button></form>
{% elif r.review_status=='auto' %}<form class=inline method=post action="/review/{{ r.id }}/revert?next={{ next|urlencode }}"><button class=bad>撤销</button></form>{% else %}<span class=mute>{{ r.reviewed_by }}</span>{% endif %}</td>
</tr>{% endfor %}</table>
<div class=pager>{% if page>1 %}<a href="?{{ qs }}&page={{ page-1 }}">上一页</a>{% endif %}<span>第 {{ page }} 页</span>{% if page*size<total %}<a href="?{{ qs }}&page={{ page+1 }}">下一页</a>{% endif %}</div>
{% endblock %}"""

CATEGORIES = """{% extends "base" %}{% block topright %}<form class=inline method=post action="/jobs/start"><input type=hidden name=kind value=params><button {{ 'disabled' if busy }}>让模型为所有叶子分类生成（跳过已有）</button></form> <form class=inline method=post action="/categories/seed"><button class=pri>挂载内置参数建议（补齐缺失）</button></form> <form class=inline method=post action="/categories/seed?replace=1" onsubmit="return confirm('用最新内置模板刷新所有“内置建议”来源的参数？人工/模型参数不受影响。')"><button>刷新内置建议</button></form> <a class=btn href="/categories/params.json">导出全部参数 JSON</a>{% endblock %}{% block body %}
<p class=mute>括号内：原分类物料数 / 新分类物料数。"关键参数（N）"点击进入编辑；已有 {{ n_with }} 个分类配置了参数（内置建议 {{ n_seed }} 个，人工/模型 {{ n_with - n_seed }} 个）。内置建议在拉取分类树后自动挂上，可直接编辑；编辑保存后来源变为“人工”，不会再被内置模板覆盖。</p>
<div class="panel tree">{% macro node(n) %}<li>{{ n.name }} <span class=cnt>({{ counts.get(n.id, 0) }} / {{ ncounts.get(n.id, 0) }})</span>
 <a href="/categories/{{ n.id }}/params" style="margin-left:8px;color:{{ '#9ca3af' if not pcounts.get(n.id) else ('#059669' if pseed.get(n.id) else '#2563eb') }}">关键参数（{{ pcounts.get(n.id, 0) }}）</a>{% if not n.children %} <a href="/categories/{{ n.id }}/ihs-map" style="margin-left:6px;color:{{ {'approved': '#059669', 'draft': '#d97706'}.get(imaps.get(n.id), '#9ca3af') }}">IHS 映射{{ {'approved': '（已确认）', 'draft': '（草稿）'}.get(imaps.get(n.id), '') }}</a>{% endif %}{% if n.children %}<ul>{% for c in n.children %}{{ node(c) }}{% endfor %}</ul>{% endif %}</li>{% endmacro %}
<ul>{% for r in roots %}{{ node(r) }}{% endfor %}</ul></div>
{% endblock %}"""

PARAMS = """{% extends "base" %}{% block topright %}<a class=btn href="/categories">← 返回分类树</a>{% endblock %}{% block body %}
<p class=mute>{{ path }} · 物料 {{ n_parts }} 条。选型这一类器件时要问的参数，以及此类器件常见的取值范围。</p>
<form method=post action="/categories/{{ cid }}/params" id=pf>
<table id=ptable><tr><th>参数</th><th>键</th><th>类型</th><th>单位</th><th>常见取值</th><th>优先级</th><th>来源</th><th></th></tr>
{% for r in rows %}<tr>
<td><input name=name value="{{ r.name }}" style="width:190px"></td><td><input name=key value="{{ r.key }}" style="width:120px"></td>
<td><select name=ptype>{% for k,l in ptypes.items() %}<option value="{{ k }}" {{ 'selected' if r.ptype==k }}>{{ l }}</option>{% endfor %}</select></td>
<td><input name=unit value="{{ r.unit }}" style="width:60px"></td>
<td><input name=vmin value="{{ r.vmin }}" style="width:90px" placeholder="最小"> – <input name=vmax value="{{ r.vmax }}" style="width:90px" placeholder="最大"><br><input name=values value="{{ r['values'] }}" style="width:200px;margin-top:3px" placeholder="多选一的取值，逗号分隔"></td>
<td><select name=priority>{% for k,l in prios.items() %}<option value="{{ k }}" {{ 'selected' if r.priority==k }}>{{ l }}</option>{% endfor %}</select></td>
<td class=mute><input type=hidden name=source value="{{ r.source }}">{{ {'model':'模型建议','seed':'内置建议'}.get(r.source, '人工') }}</td>
<td><button type=button onclick="this.closest('tr').remove()">✕</button></td></tr>{% endfor %}
</table>
<p><button type=button onclick="addRow()">＋ 添加参数</button> <button class=pri>保存参数</button>
<button formaction="/categories/{{ cid }}/params/suggest" formmethod=post {{ 'disabled' if not llm_ok }}>让模型建议（追加到表格，需保存）</button>
<button formaction="/categories/{{ cid }}/params/suggest?replace=1" formmethod=post {{ 'disabled' if not llm_ok }} onclick="return confirm('用模型建议替换现有参数？（仍需保存）')">让模型重新生成</button>
{% if not llm_ok %}<span class=mute>（设置里配置 LLM 后可用）</span>{% endif %}</p></form>
<template id=rowtpl><tr><td><input name=name style="width:190px"></td><td><input name=key style="width:120px"></td><td><select name=ptype>{% for k,l in ptypes.items() %}<option value="{{ k }}">{{ l }}</option>{% endfor %}</select></td><td><input name=unit style="width:60px"></td><td><input name=vmin style="width:90px" placeholder="最小"> – <input name=vmax style="width:90px" placeholder="最大"><br><input name=values style="width:200px;margin-top:3px" placeholder="多选一的取值"></td><td><select name=priority>{% for k,l in prios.items() %}<option value="{{ k }}">{{ l }}</option>{% endfor %}</select></td><td class=mute><input type=hidden name=source value=manual>人工</td><td><button type=button onclick="this.closest('tr').remove()">✕</button></td></tr></template>
<script>function addRow(){document.getElementById('ptable').appendChild(document.getElementById('rowtpl').content.cloneNode(true))}</script>
{% endblock %}"""

IHSMAP = """{% extends "base" %}{% block topright %}<a class=btn href="/categories">← 返回分类树</a>{% endblock %}{% block body %}
<p class=mute>{{ path }} · 可靠匹配到 IHS（L1–L3）的物料 {{ prof.n_parts }} 条 · IHS 子类：{% for c, n in prof.ihs_categories %}{{ c }}（{{ n }}）{% if not loop.last %}、{% endif %}{% endfor %}</p>
<p>状态：{% if not row %}<b>尚未生成</b>{% elif row.status == 'approved' %}<b style="color:var(--ok)">已确认</b>（{{ row.approved_by }}）{% else %}<b style="color:var(--warn)">草稿，待确认</b>{% endif %}
{% if row and row.applied_at %} · 已应用到 {{ row.applied_parts }} 个物料（{{ row.applied_at }}）{% endif %}{% if row and row.model %} · 模型 {{ row.model }}{% endif %}</p>
{% if not tgts %}<div class="msg err">该分类还没有关键参数：先到 <a href="/categories/{{ cid }}/params">关键参数</a> 挂上内置参数或让模型生成。</div>{% endif %}
<div class=filters>
<form class=inline method=post action="/categories/{{ cid }}/ihs-map/suggest"><button class=pri {{ 'disabled' if not llm_ok or not tgts or not prof.n_parts }}>{{ '让模型重新生成' if row else '让模型生成映射' }}</button></form>
{% if row and row.status != 'approved' %}<form class=inline method=post action="/categories/{{ cid }}/ihs-map/approve" onsubmit="return confirm('确认这张映射表？确认后才能应用到物料。')"><button class=ok>确认映射</button></form>{% endif %}
{% if row and row.status == 'approved' %}<form class=inline method=post action="/categories/{{ cid }}/ihs-map/apply"><button class=ok {{ 'disabled' if busy }}>应用到物料（写参数）</button></form>
<a class=btn href="/categories/{{ cid }}/part-params.csv">导出参数 CSV</a>{% endif %}
{% if not llm_ok %}<span class=mute>（设置里配置 LLM 后可生成）</span>{% endif %}</div>
{% if mapping %}<h2>映射表</h2>
<table><tr><th>ezPLM 参数</th><th>单位</th><th>方式</th><th>IHS 列</th><th>换算</th><th>取值对应</th><th>说明</th></tr>
{% for m in mapping %}<tr class="{{ 'mute' if m.kind == 'none' }}"><td><b>{{ names.get(m.target, m.target) }}</b><br><span class=mute>{{ m.target }}</span></td><td>{{ units.get(m.target, '') }}</td><td>{{ m.kind }}</td>
<td>{% if m.kind == 'range' %}{{ m.min }} ~ {{ m.max }}{% else %}{{ m.source or '' }}{% endif %}</td><td>{% if m.scale and m.scale != 1 %}× {{ m.scale }}{% endif %}</td>
<td class=mute style="max-width:220px">{% for k, v in (m.value_map or {}).items() %}{{ k }}→{{ v }}{% if not loop.last %}，{% endif %}{% endfor %}</td><td class=mute>{{ m.note }}</td></tr>{% endfor %}</table>
<h2>预览（前 {{ prev|length }} 个物料，按当前映射算出的值）</h2>
<div style="overflow-x:auto"><table><tr><th>型号</th>{% for m in mapping if m.kind != 'none' %}<th>{{ names.get(m.target, m.target) }}</th>{% endfor %}</tr>
{% for r in prev %}<tr><td><a href="/parts/{{ r.part_id }}">{{ r.mpn }}</a> <span class=mute>{{ r.level }}</span></td>{% for m in mapping if m.kind != 'none' %}<td>{{ r['values'].get(m.target, '') }}</td>{% endfor %}</tr>{% endfor %}</table></div>
<h2>手工修改（JSON）</h2><p class=mute>改完保存会变回草稿，需要重新确认。kind：number / range / enum / bool / text / none；scale 是 IHS 数值换算到 ezPLM 单位要乘的系数。</p>
<form method=post action="/categories/{{ cid }}/ihs-map/save"><textarea name=mapping_json style="width:100%;height:280px;font-family:ui-monospace,Menlo,monospace;font-size:12px">{{ mapping_text }}</textarea><p><button>保存为草稿</button></p></form>{% endif %}
<h2>IHS 参数列（覆盖率 ≥ 5%）</h2><table><tr><th>列</th><th>覆盖率</th><th>不同取值</th><th>常见值</th></tr>
{% for c in prof.columns if c.coverage >= 0.05 %}<tr><td>{{ c.column }}</td><td>{{ '%.0f'|format(c.coverage * 100) }}%</td><td>{{ c.distinct }}</td><td class=mute>{{ c.samples|join(', ') }}</td></tr>{% endfor %}</table>
{% endblock %}"""

LIBRARY = """{% extends "base" %}{% block topright %}<a class=btn href="/library/upload">导入符号 / 封装 / 工程</a>{% endblock %}{% block body %}
<div class=cards>
<div class=card><div class=n>{{ f.total }}</div><div class=l>{{ '符号' if kind=='symbols' else '封装' }}总数</div></div>
<div class=card><div class=n>{{ f.with_mpn }}</div><div class=l>已关联 MPN</div></div>
<div class=card><div class=n>{{ f.without_mpn }}</div><div class=l>未关联 MPN（通用件或待补）</div></div>
<div class=card><div class=n>{{ stats.instances }}</div><div class=l>工程元件实例（MPN 来源）</div></div>
</div>
{% if not data_mounted %}<div class="msg err">⚠ /data 不是持久化磁盘卷，索引会在重新部署时清空。请在 Railway 服务 → Settings → Volumes 挂载 /data，然后重新扫描。</div>{% endif %}
<form class=filters method=get>
<select name=kind onchange="this.form.submit()"><option value=symbols {{ 'selected' if kind=='symbols' }}>符号</option><option value=footprints {{ 'selected' if kind=='footprints' }}>封装</option></select>
<input name=q value="{{ q }}" placeholder="名称 / 库名 / MPN" style="width:220px">
<select name=has_mpn><option value="">— MPN —</option><option value=1 {{ 'selected' if has_mpn=='1' }}>有 MPN</option><option value=0 {{ 'selected' if has_mpn=='0' }}>无 MPN</option></select>
<select name=lib><option value="">— 库 / 分类 —</option>{% for l in f.libs %}<option {{ 'selected' if lib==l }}>{{ l }}</option>{% endfor %}</select>
<select name=source_kind><option value="">— 来源级别 —</option>{% for k in f.kinds %}<option {{ 'selected' if source_kind==k }}>{{ k }}</option>{% endfor %}</select>
<button class=pri>筛选</button><span class=mute>共 {{ total }} 条</span></form>
{% if kind=='symbols' %}<table><tr><th>符号</th><th>库 / 分类</th><th>引脚</th><th>默认封装</th><th>MPN 数</th><th>来源</th><th>级别</th><th>License</th></tr>
{% for r in rows %}<tr><td><a href="/library/symbols/{{ r[0] }}"><b>{{ r[1] }}</b></a></td><td>{{ r[2] }}</td><td>{{ r[6] }}</td><td class=mute>{{ r[7] }}</td><td>{{ r[10] }}</td><td class=mute style="max-width:260px;word-break:break-all">{{ r[3].split('/sources/')[-1] }}</td><td>{{ r[4] }}</td><td>{{ r[5] }}</td></tr>{% endfor %}</table>
{% else %}<table><tr><th>封装</th><th>库 / 分类</th><th>焊盘</th><th>类型</th><th>3D</th><th>MPN 数</th><th>来源</th><th>级别</th></tr>
{% for r in rows %}<tr><td><a href="/library/footprints/{{ r[0] }}"><b>{{ r[1] }}</b></a></td><td>{{ r[2] }}</td><td>{{ r[6] }}</td><td>{{ r[7] }}</td><td class=mute>{{ '有' if r[8] else '' }}</td><td>{{ r[10] }}</td><td class=mute style="max-width:260px;word-break:break-all">{{ r[3].split('/sources/')[-1] }}</td><td>{{ r[4] }}</td></tr>{% endfor %}</table>{% endif %}
<div class=pager>{% if page>1 %}<a href="?{{ qs }}&page={{ page-1 }}">上一页</a>{% endif %}<span>第 {{ page }} / {{ ((total-1)//size)+1 if total else 1 }} 页</span>{% if page*size<total %}<a href="?{{ qs }}&page={{ page+1 }}">下一页</a>{% endif %}</div>
{% endblock %}"""

LIBITEM = """{% extends "base" %}{% block topright %}<a class=btn href="/library?kind={{ kind }}">← 返回列表</a>{% endblock %}{% block body %}
<div class=grid2>
<div class=panel><h2>基本信息</h2><table class=kv>
<tr><td>库 / 分类</td><td>{{ d.lib }}</td></tr><tr><td>来源</td><td style="word-break:break-all">{{ d.source }}（{{ d.source_kind }}，commit {{ d.commit_ or '-' }}）</td></tr><tr><td>License</td><td>{{ d.license or '未知' }}</td></tr>
{% if kind=='symbols' %}<tr><td>引脚数</td><td>{{ d.pin_count }}</td></tr><tr><td>默认封装</td><td>{{ d.footprint_field }}</td></tr><tr><td>数据手册</td><td>{{ d.datasheet }}</td></tr><tr><td>派生自</td><td>{{ d.extends or '-' }}</td></tr><tr><td>关键词</td><td>{{ d.keywords }}</td></tr>
{% else %}<tr><td>焊盘数</td><td>{{ d.pad_count }}</td></tr><tr><td>类型</td><td>{{ d.attr }}</td></tr><tr><td>3D 模型</td><td>{{ d.model3d or '-' }}</td></tr><tr><td>标签</td><td>{{ d.tags }}</td></tr>{% endif %}
<tr><td>描述</td><td>{{ d.description }}</td></tr></table>
{% if kind=='symbols' and d.pins_list %}<h2>引脚（{{ d.pins_list|length }}）</h2><div style="max-height:260px;overflow:auto"><table><tr><th>编号</th><th>名称</th><th>类型</th></tr>{% for pin in d.pins_list %}<tr><td>{{ pin[0] }}</td><td>{{ pin[1] }}</td><td class=mute>{{ pin[2] }}</td></tr>{% endfor %}</table></div>{% endif %}</div>
<div class=panel><h2>关联的 MPN（{{ mpns|length }}）</h2>
<p class=mute>一个{{ '符号' if kind=='symbols' else '封装' }}可对应多个料号。来源：symbol-field = 符号自带 MPN 字段；project-instance = 开源工程里的元件实例；manual = 人工添加。</p>
<table><tr><th>MPN</th><th>制造商</th><th>来源</th><th>添加时间</th><th></th></tr>
{% for m in mpns %}<tr><td><b>{{ m[0] }}</b></td><td class=mute>{{ m[1] }}</td><td class=mute>{{ m[2] }}</td><td class=mute>{{ m[3] }}</td><td><form class=inline method=post action="/library/{{ kind }}/{{ d.rowid }}/mpn/del"><input type=hidden name=mpn value="{{ m[0] }}"><button class=bad>删除</button></form></td></tr>{% endfor %}</table>
<form class=frm method=post action="/library/{{ kind }}/{{ d.rowid }}/mpn/add"><div class=row><div><label>添加 MPN（可多个，逗号分隔）</label><input name=mpn style="width:100%" placeholder="如 MCP6004T-I/ST, MCP6004-E/SL"></div><div><label>制造商（可选）</label><input name=manufacturer></div></div><p><button class=pri>添加</button></p></form></div></div>
{% endblock %}"""

LIBUPLOAD = """{% extends "base" %}{% block topright %}<a class=btn href="/library">← 返回列表</a>{% endblock %}{% block body %}
<div class=grid2><div class=panel><h2>上传文件</h2>
<p class=mute>支持：单个 <code>.kicad_sym</code>（符号库）、<code>.kicad_mod</code>（封装）、或把整个 KiCad 工程/库目录打成的 <code>.zip</code>（会递归解析其中的 .kicad_sym / .kicad_mod / .kicad_sch / .kicad_pcb；原理图里的元件实例会自动产生 符号↔MPN、封装↔MPN 映射）。Altium / EasyEDA 请先转成 KiCad 格式再上传。</p>
<form class=frm method=post action="/library/upload" enctype=multipart/form-data>
<label>文件</label><input type=file name=file required>
<div class=row><div><label>来源标签（用于区分，如 "ST评估板-2026"）</label><input name=label required placeholder="来源名称"></div>
<div><label>可信级别</label><select name=source_kind>{% for k in ['vendor-official','kicad-official','lcsc-easyeda','digikey-library','oshw-product','community'] %}<option value="{{ k }}" {{ 'selected' if k=='oshw-product' }}>{{ k }}</option>{% endfor %}</select></div></div>
<label>License（可选，如 CC-BY-SA-4.0 / MIT）</label><input name=license>
<p><button class=pri {{ 'disabled' if busy }}>上传并解析</button></p></form></div>
<div class=panel><h2>已导入的来源</h2><table><tr><th>来源</th><th>符号</th><th>封装</th><th>实例</th></tr>{% for r in sources %}<tr><td style="word-break:break-all">{{ r[0] }}</td><td>{{ r[1] }}</td><td>{{ r[2] }}</td><td>{{ r[3] }}</td></tr>{% endfor %}</table>
<p class=mute style="margin-top:12px">批量来源（Git 仓库）请在"任务控制 → ③ 扫描"里克隆。</p></div></div>
{% endblock %}"""

EXPORT = """{% extends "base" %}{% block body %}
<div class=cards>
<div class=card><div class=n>{{ ready }}</div><div class=l>可回写（有新值、无待复核、未回写）</div></div>
<div class=card><div class=n>{{ held }}</div><div class=l>暂扣（仍有待复核建议）</div></div>
<div class=card><div class=n>{{ exported }}</div><div class=l>已回写</div></div>
</div>
<div class=grid2>
<div class=panel><h2>第一步：生成回写包</h2><p class=mute>JSONL，每行一条 PATCH 更新，只填 ezPLM 里为空的字段。生成后可下载检查。</p>
<form method=post action="/export"><button class=pri>生成回写包</button></form>
{% if result %}<p>已生成 {{ result.ready }} 条（暂扣 {{ result.held_pending_review }}）→ <a href="/export/download">下载 writeback.jsonl</a></p>{% endif %}</div>
<div class=panel><h2>第二步：正式写回 ezPLM</h2><p class=mute>需要后端提供 <code>PATCH /parts/batch</code> 接口（见 docs/API_CONTRACT.md）。写回后物料标记 exported_at，不会重复提交。</p>
<form method=post action="/export/apply" class=frm onsubmit="return confirm('确认写回 ezPLM？')"><label>输入 <b>WRITE</b> 确认</label><input name=confirm placeholder="WRITE"><p><button class=danger {{ 'disabled' if busy }}>写回 ezPLM</button></p></form></div></div>
<div class=panel><h2>回写包预览（前 20 条）</h2><pre>{{ preview or '（尚未生成）' }}</pre></div>
{% endblock %}"""

SETTINGS = """{% extends "base" %}{% block body %}
<div class=grid2>
<div class=panel><h2>ezPLM 连接</h2><form class=frm method=post action="/settings">
<label>API 地址</label><input name=api_base value="{{ cfg.api_base }}" style="width:100%">
<label>Token（留空=不修改；已保存：{{ '是' if cfg.api_token else '否' }}）{% if env.api_token %}<span class=mute>（当前由环境变量提供）</span>{% endif %}</label><input name=api_token type=password style="width:100%">
<label>每页条数（ezPLM 上限 100）</label><input name=api_page_size value="{{ cfg.api_page_size }}">
<h2 style="margin-top:18px">LLM 仲裁</h2>
<div class=row><div><label>提供商</label><select name=llm_provider><option value=gemini {{ 'selected' if cfg.llm_provider=='gemini' }}>Google Gemini</option><option value=anthropic {{ 'selected' if cfg.llm_provider=='anthropic' }}>Anthropic Claude</option><option value=openai {{ 'selected' if cfg.llm_provider=='openai' }}>OpenAI 兼容（DeepSeek/通义/智谱…）</option></select></div>
<div><label>模型（Gemini: gemini-2.5-flash；Claude: claude-haiku-4-5；DeepSeek: deepseek-chat）</label><input name=llm_model value="{{ cfg.llm_model }}" style="width:100%"></div></div>
<label>Gemini API Key（留空=不修改；已保存：{{ '是' if cfg.gemini_api_key else '否' }}）</label><input name=gemini_api_key type=password style="width:100%">
<label>Anthropic API Key（留空=不修改；已保存：{{ '是' if cfg.anthropic_api_key else '否' }}）</label><input name=anthropic_api_key type=password style="width:100%">
<div class=row><div><label>OpenAI 兼容 API Key（已保存：{{ '是' if cfg.openai_api_key else '否' }}）</label><input name=openai_api_key type=password style="width:100%"></div><div><label>OpenAI 兼容 Base URL（如 https://api.deepseek.com/v1）</label><input name=openai_base_url value="{{ cfg.openai_base_url }}" style="width:100%"></div></div>
<h2 style="margin-top:18px">阈值</h2>
<div class=row><div><label>自动写入最低置信度</label><input name=auto_fill_min value="{{ cfg.auto_fill_min }}"></div><div><label>标记疑似错误最低置信度</label><input name=flag_min value="{{ cfg.flag_min }}"></div></div>
<div class=row><div><label>拉取每批</label><input name=ingest_batch value="{{ cfg.ingest_batch }}"></div><div><label>处理每批</label><input name=process_batch value="{{ cfg.process_batch }}"></div></div>
<p><button class=pri>保存</button></p></form></div>
<div class=panel><h2>连接测试</h2><form class=inline method=post action="/settings/test"><button>测试 ezPLM API</button></form> <form class=inline method=post action="/settings/test-llm"><button>测试 LLM</button></form>{% if test %}<pre>{{ test }}</pre>{% endif %}
<h2 style="margin-top:18px">运行环境</h2><table class=kv>
<tr><td>数据库</td><td>{{ dbinfo }}</td></tr><tr><td>数据目录</td><td>{{ data_dir }}{% if not data_mounted %} <span style="color:var(--bad)">⚠ 不是持久化磁盘卷：每次重新部署会清空符号/封装索引和克隆的库。请在 Railway 服务 → Settings → Volumes 挂载到 {{ data_dir }}</span>{% endif %}</td></tr><tr><td>登录保护</td><td>{{ '已开启（ADMIN_PASSWORD）' if auth_on else '未开启——部署到公网前请设置 ADMIN_PASSWORD' }}</td></tr>
<tr><td>规则文件</td><td>config/rules.yaml（分类）、config/packages.yaml（封装映射）、config/manufacturers.yaml（URL 模板）——改完提交到 GitHub 重新部署即可生效</td></tr></table></div></div>
{% endblock %}"""

env = Environment(loader=DictLoader({"base": BASE, "home": HOME, "jobs": JOBS, "parts": PARTS, "part": PART, "review": REVIEW,
                                     "categories": CATEGORIES, "params": PARAMS, "library": LIBRARY, "libitem": LIBITEM, "libupload": LIBUPLOAD, "export": EXPORT, "settings": SETTINGS, "ihsmap": IHSMAP}), autoescape=True)
env.globals["pct"] = lambda a, b: round(100.0 * (a or 0) / b, 1) if b else 0


def render(name, request: Request | None = None, **ctx):
    ctx.setdefault("busy", manager.is_busy())
    if request is not None:
        ctx.setdefault("msg", request.query_params.get("msg", ""))
        ctx.setdefault("err", request.query_params.get("err", ""))
    return HTMLResponse(env.get_template(name).render(**ctx))


def back(url: str, msg: str = "", err: bool = False):
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}msg={msg}{'&err=1' if err else ''}" if msg else url, status_code=303)


# =========================================================================== 页面
@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: str = Depends(auth)):
    job = manager.current.to_dict() if manager.current else None
    return render("home", request, title="工作台", nav="home", user=user, s=pipeline.summary(), job=job)


# ---- 任务
def _data_mounted() -> bool:
    d = str(settings.DATA_DIR)
    if d in (".", "/app", "/home/claude", "/home/claude/ezplm_pilot") or not d.startswith("/data"):
        return True          # 本地运行不提示
    return os.path.ismount(d)


def _lib_stats():
    from .kicad import LibIndex
    try:
        return LibIndex(settings.LIBINDEX_PATH).stats()
    except Exception:
        return {"symbols": 0, "footprints": 0, "instances": 0}


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, user: str = Depends(auth)):
    job = manager.current.to_dict() if manager.current else None
    sources = sorted(p.name for p in settings.SOURCES_DIR.iterdir() if p.is_dir()) if settings.SOURCES_DIR.exists() else []
    from .ihs import IhsMatcher
    from .staging import IhsParamMap, PartParam
    dbj = get_session()
    imap = dict(dbj.execute(select(IhsParamMap.status, func.count()).group_by(IhsParamMap.status)).all())
    n_pparams = dbj.execute(select(func.count(PartParam.id))).scalar() or 0
    return render("jobs", request, title="任务控制", nav="jobs", user=user, s=pipeline.summary(), cfg=settings.get_all(), job=job, ihs_n=IhsMatcher.n_ref(dbj),
                  imap=imap, n_pparams=n_pparams, ezc=_ez_counts(dbj),
                  history=[h.to_dict() for h in manager.history], lib=_lib_stats(), sources=sources,
                  status_zh={"queued": "排队中", "running": "运行中", "done": "已完成", "failed": "失败", "stopped": "已停止"})


def _run_ingest(job, source, batch, max_batches, reset, incremental=False):
    from .sources import SampleSource
    src = settings.api_source() if source == "api" else SampleSource()
    return pipeline.ingest(src, batch=batch, max_batches=max_batches, reset=reset, job=job.id, incremental=incremental,
                           stop=job.should_stop, log=job.say, progress=lambda p: job.progress.update(p))


def _run_process(job, batch, max_batches, llm, urls, details=True):
    cfg = settings.apply_env()
    return pipeline.process(batch=batch, max_batches=max_batches, use_llm=llm, do_urls=urls, job=job.id, fetch_details=details,
                            stop=job.should_stop, log=job.say, progress=lambda p: job.progress.update(p),
                            auto_fill_min=float(cfg["auto_fill_min"]), flag_min=float(cfg["flag_min"]))


def _run_scan(job, path, source_kind):
    from .kicad import LibIndex, scan
    if re.match(r"^(https?://|git@)", path):
        name = re.sub(r"[^A-Za-z0-9_.-]", "-", path.rstrip("/").split("/")[-1].removesuffix(".git"))
        dest = settings.SOURCES_DIR / name
        if dest.exists():
            job.say(f"目录已存在，更新：{dest}")
            subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"], capture_output=True, text=True, timeout=600)
        else:
            job.say(f"git clone --depth 1 {path} → {dest}")
            r = subprocess.run(["git", "clone", "--depth", "1", path, str(dest)], capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-500:])
        path = str(dest)
    job.say(f"扫描 {path}（{source_kind}）…")
    res = scan(path, source_kind)
    idx = LibIndex(settings.LIBINDEX_PATH)
    idx.add(res)
    for e in res["errors"][:10]:
        job.say(f"  ! {e['file']}: {e['error']}")
    return {"symbols": len(res["symbols"]), "footprints": len(res["footprints"]), "instances": len(res["instances"]), "errors": len(res["errors"]), "index": idx.stats()}


def _ez_counts(db) -> dict:
    from .ezplm_lib import EzLibrary
    try:
        return EzLibrary.count(db)
    except Exception:  # noqa
        return {"companies": 0, "symbols": 0, "footprints": 0}


def _run_ezplm_lib(job):
    from .ezplm_lib import overlap_with_kicad, pull
    from .kicad import LibIndex
    from .staging import get_session as new_session
    db = new_session()
    try:
        res = pull(settings.api_source(), db, log=job.say, stop=job.should_stop)
        idx = LibIndex(settings.LIBINDEX_PATH) if settings.LIBINDEX_PATH.exists() else None
        res["overlap"] = overlap_with_kicad(db, idx)
        job.say(f"与平台已扫描的 KiCad 库按名称重合：{res['overlap']}")
        return res
    finally:
        db.close()


def _run_ihs(job, all_parts):
    cfg = settings.get_all()
    return pipeline.ihs_match(only_matched=not all_parts, job=job.id, stop=job.should_stop, log=job.say,
                              progress=lambda p: job.progress.update(p), auto_fill_min=float(cfg["auto_fill_min"]))


def _run_export(job, apply):
    api = settings.api_source() if apply else None
    return pipeline.export(apply=apply, api=api, job=job.id, stop=job.should_stop, log=job.say, progress=lambda p: job.progress.update(p))


@app.post("/jobs/start")
async def jobs_start(request: Request, user: str = Depends(auth)):
    f = await request.form()
    kind = f.get("kind")
    try:
        mb = int(f["max_batches"]) if f.get("max_batches") else None
        if kind == "ingest":
            manager.start("ingest", lambda j: _run_ingest(j, f.get("source", "api"), int(f.get("batch") or 500), mb, bool(f.get("reset")), bool(f.get("incremental"))),
                          {"source": f.get("source"), "batch": f.get("batch"), "max_batches": mb, "reset": bool(f.get("reset")), "incremental": bool(f.get("incremental"))})
        elif kind == "process":
            manager.start("process", lambda j: _run_process(j, int(f.get("batch") or 200), mb, bool(f.get("llm")), bool(f.get("urls")), bool(f.get("details"))),
                          {"batch": f.get("batch"), "max_batches": mb, "llm": bool(f.get("llm")), "urls": bool(f.get("urls")), "details": bool(f.get("details"))})
        elif kind == "scan":
            if not f.get("path"):
                return back("/jobs", "请填写仓库地址或目录", True)
            manager.start("scan", lambda j: _run_scan(j, f["path"].strip(), f.get("source_kind", "community")), {"path": f["path"], "kind": f.get("source_kind")})
        elif kind == "ezplm-lib":
            manager.start("ezplm-lib", _run_ezplm_lib, {})
        elif kind == "ihs-maps":
            from . import ihs_params as IP
            call = _llm_call_or_none()
            if not call:
                return back("/jobs", "请先在设置里配置 LLM", True)
            path_re, mp = (f.get("path_re") or "").strip() or ".", int(f.get("min_parts") or 20)
            manager.start("ihs-maps", lambda j: IP.bulk_suggest(call, path_re, mp, model=settings.get("llm_model"), stop=j.should_stop, log=j.say,
                                                                progress=lambda p: j.progress.update(p)), {"path_re": path_re, "min_parts": mp})
        elif kind == "ihs-params":
            from . import ihs_params as IP
            manager.start("ihs-params", lambda j: IP.bulk_apply(stop=j.should_stop, log=j.say, progress=lambda p: j.progress.update(p)), {})
        elif kind == "ihs":
            manager.start("ihs", lambda j: _run_ihs(j, bool(f.get("all"))), {"all": bool(f.get("all"))})
        elif kind == "export":
            manager.start("export", lambda j: _run_export(j, False), {"apply": False})
        elif kind == "params":
            from . import params as P
            call = _llm_call_or_none()
            if not call:
                return back("/categories", "请先在设置里配置 LLM", True)
            manager.start("params", lambda j: P.bulk_generate(call, only_missing=True, stop=j.should_stop, log=j.say, progress=lambda p: j.progress.update(p)), {"only_missing": True})
            return back("/jobs", "已启动：为所有叶子分类生成关键参数建议")
        else:
            return back("/jobs", "未知任务类型", True)
    except RuntimeError as e:
        return back("/jobs", str(e), True)
    return back("/jobs", f"已启动 {kind}")


@app.post("/jobs/stop")
def jobs_stop(user: str = Depends(auth)):
    return back("/jobs", "已发送停止信号" if manager.stop() else "没有运行中的任务")


@app.post("/jobs/reset-processed")
def jobs_reset(user: str = Depends(auth)):
    db = get_session()
    n = db.query(StagedPart).filter(StagedPart.processed_at.isnot(None)).update({StagedPart.processed_at: None})
    db.commit()
    v = pipeline.void_stale_pending()
    return back("/jobs", f"已重置 {n} 条为未处理，作废其待复核建议 {v} 条")


@app.get("/api/jobs/current")
def api_job(user: str = Depends(auth)):
    return JSONResponse(manager.current.to_dict() if manager.current else None)


# ---- 物料
@app.get("/parts", response_class=HTMLResponse)
def parts(request: Request, q: str = "", missing: str = "", changed: str = "", processed: str = "", tag: str = "", page: int = 1, size: int = 50, user: str = Depends(auth)):
    db = get_session()
    stmt = select(StagedPart)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(StagedPart.mpn.ilike(like), StagedPart.manufacturer.ilike(like), StagedPart.description.ilike(like)))
    col = {"category": StagedPart.orig_category_id, "symbol": StagedPart.orig_symbol, "footprint": StagedPart.orig_footprint, "url": StagedPart.orig_url}.get(missing)
    if col is not None:
        stmt = stmt.where(or_(col == "", col.is_(None)))
    elif missing == "pdf":
        stmt = stmt.where(StagedPart.orig_has_pdf.is_(False))
    anynew = or_(*[getattr(StagedPart, col).isnot(None) for _, col in pipeline.EXPORT_FIELDS])
    if changed == "1":
        stmt = stmt.where(anynew)
    elif changed == "0":
        stmt = stmt.where(~anynew)
    if processed == "1":
        stmt = stmt.where(StagedPart.processed_at.isnot(None))
    elif processed == "0":
        stmt = stmt.where(StagedPart.processed_at.is_(None))
    if tag:
        stmt = stmt.where(StagedPart.tags.ilike(f"%{tag}%"))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar()
    rows = db.execute(stmt.order_by(StagedPart.value_score.desc(), StagedPart.mpn).offset((page - 1) * size).limit(size)).scalars().all()
    tax = pipeline.load_taxonomy(db)
    paths = {cid: tax.path(cid) for cid in {r.orig_category_id for r in rows} | {r.new_category_id for r in rows} if cid and cid in tax.nodes}
    sugg: dict = {}
    for s_ in db.execute(select(StagedProposal).where(StagedProposal.part_id.in_([r.id for r in rows]), StagedProposal.review_status == "pending").order_by(StagedProposal.confidence.desc())).scalars():
        sugg.setdefault(s_.part_id, []).append(s_) if len(sugg.get(s_.part_id, [])) < 2 else None
    qs = f"q={q}&missing={missing}&changed={changed}&processed={processed}&tag={tag}"
    return render("parts", request, title="物料信息", nav="parts", user=user, rows=rows, total=total, page=page, size=size, q=q, missing=missing, changed=changed, processed=processed, tag=tag, qs=qs, paths=paths, sugg=sugg)


@app.get("/parts/{pid}", response_class=HTMLResponse)
def part(request: Request, pid: str, user: str = Depends(auth)):
    db = get_session()
    p = db.get(StagedPart, pid)
    if not p:
        raise HTTPException(404)
    props = db.execute(select(StagedProposal).where(StagedProposal.part_id == pid).order_by(StagedProposal.field, StagedProposal.confidence.desc())).scalars().all()
    tax = pipeline.load_taxonomy(db)
    from .ihs import IhsMatcher, reference_rows
    from .staging import PartParam
    pparams = db.execute(select(PartParam).where(PartParam.part_id == pid).order_by(PartParam.id)).scalars().all()
    pnames = {}
    if pparams:
        from . import ihs_params as IP
        pnames = {t["key"]: t["name"] for t in IP.targets(db, pparams[0].category_id)}
    return render("part", request, title=p.mpn, nav="parts", user=user, p=p, props=props, tax=tax, leaves=tax.leaf_labels(),
                  ihs=reference_rows(db, p, tax), ihs_n=IhsMatcher.n_ref(db), pparams=pparams, pnames=pnames)


@app.post("/parts/{pid}/edit")
async def part_edit(request: Request, pid: str, user: str = Depends(auth)):
    f = await request.form()
    db = get_session()
    p = db.get(StagedPart, pid)
    if not p:
        raise HTTPException(404)
    tax = pipeline.load_taxonomy(db)
    cp = (f.get("new_category_path") or "").strip()
    if cp == "-":
        p.new_category_id, p.new_category = None, None
    elif cp:
        hit = [cid for cid, path in tax.leaf_labels() if path == cp or path.endswith(" > " + cp)]
        if not hit:
            return back(f"/parts/{pid}", "分类未找到，请从下拉列表中选择", True)
        p.new_category_id, p.new_category = hit[0], tax.nodes[hit[0]].name
    for k in ("new_symbol", "new_footprint", "new_url", "new_datasheet", "new_description_en", "new_manufacturer"):
        v = (f.get(k) or "").strip()
        setattr(p, k, v or None)
    p.tags = ",".join(t.strip() for t in (f.get("tags") or "").replace("，", ",").split(",") if t.strip())
    if p.tags and "人工修改" not in p.tags:
        p.tags += ",人工修改"
    elif not p.tags:
        p.tags = "人工修改"
    db.add(StagedProposal(part_id=pid, mpn=p.mpn, field="manual", current="", proposed=json.dumps({k: f.get(k) for k in f.keys()}, ensure_ascii=False)[:480],
                          proposed_label="人工修改", confidence=1.0, action="manual", method="manual", evidence_json="{}", tags="人工修改",
                          review_status="approved", reviewed_by=user, reviewed_at=datetime.utcnow(), job="manual"))
    if f.get("reprocess"):
        p.processed_at = None
    db.commit()
    return back(f"/parts/{pid}", "已保存")


# ---- 复核
@app.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, field: str = "", action: str = "", status: str = "pending", minconf: str = "", page: int = 1, size: int = 50, user: str = Depends(auth)):
    db = get_session()
    stmt = select(StagedProposal, StagedPart).join(StagedPart, StagedPart.id == StagedProposal.part_id).where(StagedProposal.review_status == status, StagedProposal.field != "manual")
    if field:
        stmt = stmt.where(StagedProposal.field == field)
    if action:
        stmt = stmt.where(StagedProposal.action == action)
    if minconf:
        stmt = stmt.where(StagedProposal.confidence >= float(minconf))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar()
    rows = db.execute(stmt.order_by(StagedProposal.confidence.desc(), StagedPart.value_score.desc()).offset((page - 1) * size).limit(size)).all()
    tax = pipeline.load_taxonomy(db)
    for r, p in rows:
        r.current_label = tax.path(r.current) if (r.field == "category_id" and r.current in tax.nodes) else ""
    qs = f"field={field}&action={action}&status={status}&minconf={minconf}"
    return render("review", request, title="复核队列", nav="review", user=user, rows=rows, total=total, page=page, size=size, field=field, action=action, status=status, minconf=minconf, qs=qs, next=f"/review?{qs}&page={page}")


@app.post("/review/bulk")
async def review_bulk(request: Request, next: str = "", user: str = Depends(auth)):
    f = await request.form()
    n = 0
    for rid in (f.get("ids") or "").split(","):
        if rid.strip():
            try:
                pipeline.review(int(rid), "approve", user=user); n += 1
            except KeyError:
                pass
    return back(next or "/review", f"已通过 {n} 条")


@app.post("/review/void-stale")
def review_void_stale(user: str = Depends(auth)):
    return back("/review", f"已作废未处理物料上的旧建议 {pipeline.void_stale_pending()} 条")


@app.post("/review/dedupe")
def review_dedupe(user: str = Depends(auth)):
    return back("/review", f"已作废重复建议 {pipeline.dedupe_pending()} 条")


@app.post("/review/{rid}/{decision}")
def review_do(rid: int, decision: str, next: str = "", user: str = Depends(auth)):
    if decision not in ("approve", "reject", "revert"):
        raise HTTPException(400)
    pr = pipeline.review(rid, decision, user=user)
    return RedirectResponse(next or f"/parts/{pr.part_id}", status_code=303)


# ---- 分类 / 库
@app.get("/categories", response_class=HTMLResponse)
def categories(request: Request, user: str = Depends(auth)):
    from . import params as P
    db = get_session()
    tax = pipeline.load_taxonomy(db)
    counts = dict(db.execute(select(StagedPart.orig_category_id, func.count()).group_by(StagedPart.orig_category_id)).all())
    ncounts = dict(db.execute(select(StagedPart.new_category_id, func.count()).where(StagedPart.new_category_id.isnot(None)).group_by(StagedPart.new_category_id)).all())
    pcounts = P.counts(db)
    pseed = dict(db.execute(select(CategoryParam.category_id, func.count()).where(CategoryParam.source == "seed").group_by(CategoryParam.category_id)).all())

    def build(nid):
        n = tax.nodes[nid]
        return {"name": n.name, "id": n.id, "children": [build(c) for c in n.children]}
    roots = [build(n.id) for n in tax.nodes.values() if not n.parent]
    from .staging import IhsParamMap
    imaps = dict(db.execute(select(IhsParamMap.category_id, IhsParamMap.status)).all())
    return render("categories", request, title="分类与关键参数", nav="categories", user=user, roots=roots, counts=counts, ncounts=ncounts, pcounts=pcounts, pseed=pseed, n_with=len(pcounts), n_seed=len(pseed), imaps=imaps)


@app.post("/categories/seed")
def categories_seed(replace: str = "", user: str = Depends(auth)):
    from . import params as P
    try:
        st = P.seed_load(get_session(), replace_seed=bool(replace))
    except Exception as e:  # noqa
        return back("/categories", f"挂载失败：{str(e)[:150]}", True)
    return back("/categories", f"已为 {st['seeded']} 个分类挂上内置参数建议，跳过 {st['skipped']} 个（已有人工/模型参数）")


@app.on_event("startup")
def _seed_on_startup():
    """启动时自动给没有参数的叶子分类挂上内置建议（分类树尚未拉取时静默跳过）。"""
    try:
        from . import params as P
        db = _new_session()
        st = P.seed_load(db)
        db.close()
        if st["seeded"]:
            print(f"[params] 内置参数建议已挂到 {st['seeded']} 个分类")
    except Exception as e:  # noqa
        print("[params] seed skipped:", e)


def _llm_call_or_none():
    from .classify import LlmArbiter
    cfg = settings.apply_env()
    arb = LlmArbiter(pipeline.load_taxonomy(get_session()), provider=cfg["llm_provider"], model=cfg["llm_model"])
    return arb.call if arb.available else None


@app.get("/categories/params.json")
def params_export(user: str = Depends(auth)):
    from . import params as P
    db = get_session()
    return JSONResponse(P.export_all(db, pipeline.load_taxonomy(db)), headers={"Content-Disposition": "attachment; filename=category_params.json"})


@app.get("/categories/{cid}/params", response_class=HTMLResponse)
def params_page(request: Request, cid: str, user: str = Depends(auth)):
    from . import params as P
    db = get_session()
    tax = pipeline.load_taxonomy(db)
    if cid not in tax.nodes:
        raise HTTPException(404)
    rows = [dict(name=r.name, key=r.key, ptype=r.ptype, unit=r.unit, vmin=r.vmin, vmax=r.vmax, values=r.values, priority=r.priority, source=r.source) for r in P.list_params(db, cid)]
    extra = request.query_params.get("suggest")
    if extra:
        try:
            sug = json.loads(_SUGGEST_CACHE.pop(extra, "[]"))
            rows = (rows if request.query_params.get("mode") != "replace" else []) + sug
        except Exception:
            pass
    n_parts = db.execute(select(func.count(StagedPart.id)).where((StagedPart.new_category_id == cid) | (StagedPart.orig_category_id == cid))).scalar()
    return render("params", request, title=f"关键参数 · {tax.nodes[cid].name}", nav="categories", user=user, cid=cid, path=tax.path(cid), rows=rows,
                  ptypes=P.PTYPES, prios=P.PRIORITIES, n_parts=n_parts, llm_ok=_llm_call_or_none() is not None)


_SUGGEST_CACHE: dict = {}


# ---- IHS 参数映射（分类级）
@app.get("/categories/{cid}/ihs-map", response_class=HTMLResponse)
def ihs_map_page(request: Request, cid: str, user: str = Depends(auth)):
    from . import ihs_params as IP
    from .staging import IhsParamMap
    db = get_session()
    tax = pipeline.load_taxonomy(db)
    if cid not in tax.nodes:
        raise HTTPException(404)
    row = db.get(IhsParamMap, cid)
    mapping = json.loads(row.mapping_json) if row and row.mapping_json else []
    tgts = IP.targets(db, cid)
    return render("ihsmap", request, title=f"IHS 参数映射 · {tax.nodes[cid].name}", nav="categories", user=user, cid=cid, path=tax.path(cid),
                  row=row, mapping=mapping, mapping_text=json.dumps(mapping, ensure_ascii=False, indent=1), tgts=tgts,
                  names={t["key"]: t["name"] for t in tgts}, units={t["key"]: t["unit"] for t in tgts},
                  prof=IP.profile(db, cid), prev=IP.preview(db, cid, mapping) if mapping else [], llm_ok=_llm_call_or_none() is not None)


@app.post("/categories/{cid}/ihs-map/suggest")
def ihs_map_suggest(cid: str, user: str = Depends(auth)):
    from . import ihs_params as IP
    call = _llm_call_or_none()
    if not call:
        return back(f"/categories/{cid}/ihs-map", "请先在设置里配置 LLM", True)
    db = get_session()
    try:
        IP.suggest(db, pipeline.load_taxonomy(db), cid, call, model=settings.get("llm_model"))
    except Exception as e:  # noqa
        return back(f"/categories/{cid}/ihs-map", f"生成失败：{str(e)[:200]}", True)
    return back(f"/categories/{cid}/ihs-map", "已生成映射草稿，请检查后确认")


@app.post("/categories/{cid}/ihs-map/save")
async def ihs_map_save(request: Request, cid: str, user: str = Depends(auth)):
    from . import ihs_params as IP
    from .staging import IhsParamMap
    f = await request.form()
    db = get_session()
    try:
        prof = IP.profile(db, cid)
        mapping = IP.parse_mapping(f.get("mapping_json") or "", IP.targets(db, cid), {c["column"] for c in prof["columns"]})
    except Exception as e:  # noqa
        return back(f"/categories/{cid}/ihs-map", f"JSON 有误：{str(e)[:200]}", True)
    row = db.get(IhsParamMap, cid) or IhsParamMap(category_id=cid, n_parts=prof["n_parts"], model="manual")
    row.mapping_json, row.status, row.approved_by, row.updated_at = json.dumps(mapping, ensure_ascii=False), "draft", None, datetime.utcnow()
    db.merge(row)
    db.commit()
    return back(f"/categories/{cid}/ihs-map", "已保存为草稿（引用了不存在的列的项已置为 none）")


@app.post("/categories/{cid}/ihs-map/approve")
def ihs_map_approve(cid: str, user: str = Depends(auth)):
    from .staging import IhsParamMap
    db = get_session()
    row = db.get(IhsParamMap, cid)
    if not row:
        raise HTTPException(404)
    row.status, row.approved_by = "approved", user
    db.commit()
    return back(f"/categories/{cid}/ihs-map", "已确认，可以应用到物料")


@app.post("/categories/{cid}/ihs-map/apply")
def ihs_map_apply(cid: str, user: str = Depends(auth)):
    from . import ihs_params as IP
    from .staging import get_session as new_session
    def run(j):
        db = new_session()
        try:
            return IP.apply(db, cid, log=j.say)
        finally:
            db.close()
    try:
        manager.start("ihs-params", run, {"category": cid})
    except RuntimeError as e:
        return back(f"/categories/{cid}/ihs-map", str(e), True)
    return back("/jobs", "已启动：按映射给该分类物料写参数")


@app.get("/categories/{cid}/part-params.csv")
def part_params_csv(cid: str, user: str = Depends(auth)):
    import csv, io
    from . import ihs_params as IP
    from .staging import PartParam
    db = get_session()
    names = {t["key"]: t["name"] for t in IP.targets(db, cid)}
    rows = db.execute(select(StagedPart.id, StagedPart.mpn, StagedPart.manufacturer, PartParam.key, PartParam.value, PartParam.num, PartParam.unit, PartParam.detail)
                      .join(PartParam, PartParam.part_id == StagedPart.id).where(PartParam.category_id == cid).order_by(StagedPart.mpn, PartParam.key)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["part_id", "mpn", "manufacturer", "param_key", "param_name", "value", "number", "unit", "source"])
    for pid, mpn, mf, k, v, num, unit, det in rows:
        w.writerow([pid, mpn, mf, k, names.get(k, k), v, "" if num is None else num, unit, det])
    return HTMLResponse("\ufeff" + buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=part_params_{cid}.csv"})


@app.post("/categories/{cid}/params")
async def params_save(request: Request, cid: str, user: str = Depends(auth)):
    from . import params as P
    f = await request.form()
    cols = ("name", "key", "ptype", "unit", "vmin", "vmax", "values", "priority", "source")
    lists = {c: f.getlist(c) for c in cols}
    n = max(len(v) for v in lists.values()) if lists else 0
    rows = [{c: (lists[c][i] if i < len(lists[c]) else "") for c in cols} for i in range(n)]
    P.save_params(get_session(), cid, rows)
    return back(f"/categories/{cid}/params", f"已保存 {len([r for r in rows if r['name'].strip()])} 个参数")


@app.post("/categories/{cid}/params/suggest")
def params_suggest(cid: str, replace: str = "", user: str = Depends(auth)):
    from . import params as P
    call = _llm_call_or_none()
    if not call:
        return back(f"/categories/{cid}/params", "未配置 LLM", True)
    db = get_session()
    tax = pipeline.load_taxonomy(db)
    try:
        rows = P.suggest_params(tax, cid, call, P._sample_descriptions(db, cid))
    except Exception as e:  # noqa
        return back(f"/categories/{cid}/params", f"模型建议失败：{str(e)[:150]}", True)
    token = secrets.token_hex(6)
    _SUGGEST_CACHE[token] = json.dumps(rows, ensure_ascii=False)
    return RedirectResponse(f"/categories/{cid}/params?suggest={token}&mode={'replace' if replace else 'append'}&msg=模型给出 {len(rows)} 个参数，请检查后点保存", status_code=303)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", kind: str = "symbols", has_mpn: str = "", lib: str = "", source_kind: str = "", page: int = 1, size: int = 50, user: str = Depends(auth)):
    from .kicad import LibIndex
    idx = LibIndex(settings.LIBINDEX_PATH)
    total, rows = idx.list_items(kind, q, has_mpn, lib, source_kind, page, size)
    qs = f"kind={kind}&q={q}&has_mpn={has_mpn}&lib={lib}&source_kind={source_kind}"
    return render("library", request, title="符号 / 封装库", nav="library", user=user, rows=rows, total=total, page=page, size=size, q=q, kind=kind,
                  has_mpn=has_mpn, lib=lib, source_kind=source_kind, f=idx.facets(kind), stats=idx.stats(), qs=qs, data_mounted=_data_mounted())


@app.get("/library/upload", response_class=HTMLResponse)
def library_upload_page(request: Request, user: str = Depends(auth)):
    from .kicad import LibIndex
    idx = LibIndex(settings.LIBINDEX_PATH)
    src = idx.con.execute("""select src, sum(s) as s, sum(f) as f, sum(i) as i from (
        select substr(source, 1, instr(source || '/', '/sources/') + 40) as src, 1 as s, 0 as f, 0 as i from symbols
        union all select substr(source, 1, instr(source || '/', '/sources/') + 40), 0, 1, 0 from footprints
        union all select substr(source, 1, instr(source || '/', '/sources/') + 40), 0, 0, 1 from instances) group by src order by s desc""").fetchall()
    sources = []
    for r in src:
        seg = r[0].split("/sources/")[-1].split("/")[0] if "/sources/" in r[0] else r[0]
        sources.append((seg, r[1], r[2], r[3]))
    merged: dict = {}
    for name, a, b, c in sources:
        m = merged.setdefault(name, [0, 0, 0]); m[0] += a; m[1] += b; m[2] += c
    return render("libupload", request, title="导入符号 / 封装", nav="library", user=user, sources=[(k, *v) for k, v in merged.items()])


@app.post("/library/upload")
async def library_upload(request: Request, user: str = Depends(auth)):
    import zipfile
    form = await request.form()
    up = form.get("file")
    label = re.sub(r"[^A-Za-z0-9_.一-龥-]", "-", (form.get("label") or "upload").strip())[:60]
    kind = form.get("source_kind", "community")
    lic = (form.get("license") or "").strip()
    if up is None or not getattr(up, "filename", ""):
        return back("/library/upload", "请选择文件", True)
    dest = settings.SOURCES_DIR / "uploads" / label
    dest.mkdir(parents=True, exist_ok=True)
    fname = Path(up.filename).name
    data = await up.read()
    if fname.lower().endswith(".zip"):
        tmp = dest / fname
        tmp.write_bytes(data)
        with zipfile.ZipFile(tmp) as z:
            for m in z.infolist():
                n = m.filename
                if n.startswith("__MACOSX") or ".." in n or n.startswith("/"):
                    continue
                if n.endswith(("/",)):
                    continue
                if not n.lower().endswith((".kicad_sym", ".kicad_mod", ".kicad_sch", ".kicad_pcb", ".kicad_pro", ".wrl", ".step", ".stp")):
                    continue
                target = dest / n
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(m))
        tmp.unlink(missing_ok=True)
    elif fname.lower().endswith((".kicad_sym", ".kicad_mod", ".kicad_sch", ".kicad_pcb")):
        sub = dest / ("footprints.pretty" if fname.lower().endswith(".kicad_mod") else "")
        sub.mkdir(parents=True, exist_ok=True)
        (sub / fname).write_bytes(data)
    else:
        return back("/library/upload", "不支持的文件类型", True)
    if lic:
        (dest / "LICENSE").write_text(lic, encoding="utf-8")

    def run(job):
        from .kicad import LibIndex, scan
        job.say(f"解析 {dest} …")
        res = scan(dest, kind)
        idx = LibIndex(settings.LIBINDEX_PATH)
        idx.add(res)
        for e in res["errors"][:10]:
            job.say(f"  ! {e['file']}: {e['error']}")
        return {"symbols": len(res["symbols"]), "footprints": len(res["footprints"]), "instances": len(res["instances"]), "errors": len(res["errors"]), "index": idx.stats()}
    try:
        manager.start("scan-upload", run, {"label": label, "file": fname, "kind": kind})
    except RuntimeError as e:
        return back("/library/upload", str(e), True)
    return back("/jobs", f"已上传 {fname}，正在解析入库")


@app.get("/library/{kind}/{rowid}", response_class=HTMLResponse)
def library_item(request: Request, kind: str, rowid: int, user: str = Depends(auth)):
    from .kicad import LibIndex
    if kind not in ("symbols", "footprints"):
        raise HTTPException(404)
    idx = LibIndex(settings.LIBINDEX_PATH)
    d = idx.get_item(kind, rowid)
    if not d:
        raise HTTPException(404)
    return render("libitem", request, title=f"{'符号' if kind=='symbols' else '封装'} · {d['name']}", nav="library", user=user, kind=kind, d=d, mpns=idx.mpns(kind, rowid))


@app.post("/library/{kind}/{rowid}/mpn/{op}")
async def library_mpn(request: Request, kind: str, rowid: int, op: str, user: str = Depends(auth)):
    from .kicad import LibIndex
    f = await request.form()
    idx = LibIndex(settings.LIBINDEX_PATH)
    if op == "add":
        n = 0
        for m in re.split(r"[,，;；\n]+", f.get("mpn") or ""):
            if m.strip():
                idx.add_mpn(kind, rowid, m.strip(), (f.get("manufacturer") or "").strip(), "manual"); n += 1
        return back(f"/library/{kind}/{rowid}", f"已添加 {n} 个 MPN")
    if op == "del":
        idx.del_mpn(kind, rowid, f.get("mpn") or "")
        return back(f"/library/{kind}/{rowid}", "已删除")
    raise HTTPException(400)


# ---- 回写
def _export_ctx():
    db = get_session()
    s = pipeline.summary()
    anynew = or_(*[getattr(StagedPart, col).isnot(None) for _, col in pipeline.EXPORT_FIELDS])
    pending = {r[0] for r in db.execute(select(StagedProposal.part_id).where(StagedProposal.review_status == "pending")).all()}
    cands = [r[0] for r in db.execute(select(StagedPart.id).where(anynew, StagedPart.exported_at.is_(None))).all()]
    held = sum(1 for c in cands if c in pending)
    f = settings.OUT_DIR / "writeback.jsonl"
    preview = "".join(open(f, encoding="utf-8").readlines()[:20]) if f.exists() else ""
    return dict(ready=len(cands) - held, held=held, exported=s["parts_exported"], preview=preview)


@app.get("/export", response_class=HTMLResponse)
def export_page(request: Request, user: str = Depends(auth)):
    return render("export", request, title="回写 ezPLM", nav="export", user=user, result=None, **_export_ctx())


@app.post("/export", response_class=HTMLResponse)
def export_do(request: Request, user: str = Depends(auth)):
    result = pipeline.export(apply=False)
    return render("export", request, title="回写 ezPLM", nav="export", user=user, result=result, **_export_ctx())


@app.post("/export/apply")
def export_apply(confirm: str = Form(""), user: str = Depends(auth)):
    if confirm.strip() != "WRITE":
        return back("/export", "请输入 WRITE 确认", True)
    if not settings.get("api_token"):
        return back("/export", "请先在设置里填写 ezPLM API Token", True)
    try:
        manager.start("export-apply", lambda j: _run_export(j, True), {"apply": True})
    except RuntimeError as e:
        return back("/export", str(e), True)
    return back("/jobs", "已启动正式回写，可在此查看进度")


@app.get("/export/download")
def export_download(user: str = Depends(auth)):
    return FileResponse(settings.OUT_DIR / "writeback.jsonl", filename="writeback.jsonl")


# ---- 设置
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(auth)):
    url = settings.db_url()
    dbinfo = "SQLite：" + url.split("///")[-1] if url.startswith("sqlite") else "PostgreSQL：" + re.sub(r"//.*@", "//***@", url)
    return render("settings", request, title="设置", nav="settings", user=user, cfg=settings.get_all(), env={k: bool(os.environ.get(v)) for k, v in settings.ENV_MAP.items()},
                  test=request.query_params.get("test", ""), dbinfo=dbinfo, data_dir=str(settings.DATA_DIR), data_mounted=_data_mounted(), auth_on=bool(os.environ.get("ADMIN_PASSWORD") or os.environ.get("EZPLM_WEB_PASSWORD")))


@app.post("/settings")
async def settings_save(request: Request, user: str = Depends(auth)):
    f = await request.form()
    settings.set_many({k: (f.get(k) or "").strip() for k in settings.DEFAULTS})
    return back("/settings", "已保存")


@app.post("/settings/test")
def settings_test(user: str = Depends(auth)):
    try:
        r = settings.api_source().test()
        r["sample"] = {k: r["sample"].get(k) for k in ("id", "mpn", "description")} if r.get("sample") else None
        out = json.dumps(r, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa
        out = f"连接失败：{e}"
    return RedirectResponse(f"/settings?test={out}", status_code=303)


@app.post("/settings/test-llm")
def settings_test_llm(user: str = Depends(auth)):
    from .classify import LlmArbiter
    try:
        cfg = settings.apply_env()
        arb = LlmArbiter(pipeline.load_taxonomy(get_session()), provider=cfg["llm_provider"], model=cfg["llm_model"])
        if not arb.available:
            out = f"未配置 {cfg['llm_provider']} 的 API Key"
        else:
            txt = arb.call('只输出 JSON：{"ok": true, "model": "<你的模型名>"}')
            out = f"{cfg['llm_provider']} / {cfg['llm_model']} 响应：{txt[:300]}"
    except Exception as e:  # noqa
        out = f"LLM 调用失败：{e}"
    return RedirectResponse(f"/settings?test={out}", status_code=303)


@app.get("/api/summary")
def api_summary(user: str = Depends(auth)):
    return json.loads(json.dumps(pipeline.summary(), default=str))


@app.get("/healthz")
def healthz():
    return {"ok": True}
