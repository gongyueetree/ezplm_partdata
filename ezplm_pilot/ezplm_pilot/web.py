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
from .staging import StagedCategory, StagedPart, StagedProposal, get_session

app = FastAPI(title="ezPLM 暂存数据平台")
security = HTTPBasic(auto_error=False)


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
<div class=panel><h2>建议分布</h2>
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
<p><button class=pri {{ 'disabled' if busy }}>开始处理</button> <span class=mute>待处理 {{ s.parts_total - s.parts_processed }} 条</span></p></form>
<form method=post action="/jobs/reset-processed" onsubmit="return confirm('把所有物料标记为未处理？已有建议和新值保留。')"><button>重置处理状态（规则更新后重跑）</button></form></div>

<div class=panel><h2>③ 扫描符号/封装来源（scan）</h2>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=scan>
<label>Git 仓库地址（或服务器上的目录路径）</label><input name=path style="width:100%" placeholder="https://github.com/espressif/kicad-libraries.git">
<div class=row><div><label>来源可信级别</label><select name=source_kind>{% for k in ['kicad-official','vendor-official','lcsc-easyeda','digikey-library','oshw-product','community'] %}<option>{{ k }}</option>{% endfor %}</select></div></div>
<p><button class=pri {{ 'disabled' if busy }}>克隆并扫描</button> <span class=mute>索引：符号 {{ lib.symbols }} / 封装 {{ lib.footprints }}</span></p></form>
<div class=mute>已下载的来源：{{ sources|join('、') or '无' }}</div></div>

<div class=panel><h2>④ 回写 ezPLM（export）</h2>
<form class=frm method=post action="/jobs/start"><input type=hidden name=kind value=export>
<p><button {{ 'disabled' if busy }}>生成回写包（不写 ezPLM）</button> <a class=btn href="/export">查看 / 下载 / 正式回写 →</a></p></form></div>
</div>

<div>
<div class=panel><h2>当前任务 {% if busy %}<form class=inline method=post action="/jobs/stop"><button class=bad>停止</button></form>{% endif %}</h2>
<div id=jobbox>{% if job %}<div><b>{{ job.id }}</b> <span class="status-{{ job.status }}">{{ job.status }}</span> · 进度 {{ job.progress.done }}{% if job.progress.total %} / {{ job.progress.total }}{% endif %}</div>
<pre id=joblog>{{ job.log|join('\\n') }}</pre>{% else %}<p class=mute>无</p>{% endif %}</div></div>
<div class=panel><h2>历史任务</h2>
<table><tr><th>任务</th><th>状态</th><th>开始</th><th>结束</th><th>结果</th></tr>
{% for h in history %}<tr><td>{{ h.id }}</td><td class="status-{{ h.status }}">{{ h.status }}</td><td class=mute>{{ h.started_at }}</td><td class=mute>{{ h.finished_at }}</td><td class=mute style="max-width:300px">{{ h.result }}</td></tr>{% endfor %}</table></div>
</div></div>
{% endblock %}{% block script %}{% if busy %}<script>
setInterval(async()=>{const r=await fetch('/api/jobs/current');const j=await r.json();if(!j){location.reload();return}
document.getElementById('jobbox').innerHTML='<div><b>'+j.id+'</b> <span class="status-'+j.status+'">'+j.status+'</span> · 进度 '+j.progress.done+(j.progress.total?' / '+j.progress.total:'')+'</div><pre id=joblog>'+j.log.join('\\n').replace(/</g,'&lt;')+'</pre>';
const p=document.getElementById('joblog');p.scrollTop=p.scrollHeight;if(j.status!=='running'&&j.status!=='queued'){setTimeout(()=>location.reload(),1500)}},2000);
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
<tr><td>描述</td><td>{{ p.description }}<br><span class=mute>{{ p.description_en }}</span></td></tr>
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
<label>标签（逗号分隔）</label><input name=tags value="{{ p.tags or '' }}" style="width:100%">
<p><button class=pri>保存</button> <button name=reprocess value=1>保存并标记为待重新处理</button></p></form></div></div>
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
<select name=field><option value="">— 字段 —</option>{% for v in ['category_id','symbol','footprint','url','datasheet'] %}<option {{ 'selected' if field==v }}>{{ v }}</option>{% endfor %}</select>
<select name=action><option value="">— 动作 —</option>{% for v in ['flag_mismatch','review'] %}<option {{ 'selected' if action==v }}>{{ v }}</option>{% endfor %}</select>
<select name=status><option value=pending {{ 'selected' if status=='pending' }}>待复核</option><option value=auto {{ 'selected' if status=='auto' }}>自动写入（抽检）</option><option value=approved {{ 'selected' if status=='approved' }}>已通过</option><option value=rejected {{ 'selected' if status=='rejected' }}>已驳回</option></select>
<input name=minconf value="{{ minconf }}" placeholder="最低置信度" style="width:110px"><button class=pri>筛选</button><span class=mute>共 {{ total }} 条</span>
{% if status=='pending' and rows %}<form class=inline method=post action="/review/bulk?next={{ next|urlencode }}"><input type=hidden name=ids value="{{ rows|map(attribute='0.id')|join(',') }}"><button class=ok onclick="return confirm('通过本页全部 {{ rows|length }} 条？')">本页全部通过</button></form>{% endif %}</form>
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

CATEGORIES = """{% extends "base" %}{% block body %}
<p class=mute>括号内：原分类物料数 / 新分类物料数。分类树来自 ezPLM（ingest 时同步）。</p>
<div class="panel tree">{% macro node(n) %}<li>{{ n.name }} <span class=cnt>({{ counts.get(n.id, 0) }} / {{ ncounts.get(n.id, 0) }})</span>{% if n.children %}<ul>{% for c in n.children %}{{ node(c) }}{% endfor %}</ul>{% endif %}</li>{% endmacro %}
<ul>{% for r in roots %}{{ node(r) }}{% endfor %}</ul></div>
{% endblock %}"""

LIBRARY = """{% extends "base" %}{% block body %}
<form class=filters method=get><input name=q value="{{ q }}" placeholder="符号/封装名、库名" style="width:260px"><select name=kind><option value=symbols {{ 'selected' if kind=='symbols' }}>符号</option><option value=footprints {{ 'selected' if kind=='footprints' }}>封装</option></select><button class=pri>搜索</button><span class=mute>索引：符号 {{ stats.symbols }} / 封装 {{ stats.footprints }} / 工程元件实例 {{ stats.instances }}</span></form>
{% if kind=='symbols' %}<table><tr><th>符号</th><th>库</th><th>引脚数</th><th>默认封装</th><th>MPN 字段</th><th>来源</th><th>可信级</th><th>License</th></tr>
{% for r in rows %}<tr><td><b>{{ r[0] }}</b></td><td>{{ r[1] }}</td><td>{{ r[2] }}</td><td class=mute>{{ r[3] }}</td><td class=mute>{{ r[4] }}</td><td class=mute style="max-width:300px;word-break:break-all">{{ r[5] }}</td><td>{{ r[6] }}</td><td>{{ r[7] }}</td></tr>{% endfor %}</table>
{% else %}<table><tr><th>封装</th><th>库</th><th>焊盘数</th><th>类型</th><th>3D</th><th>来源</th><th>可信级</th></tr>
{% for r in rows %}<tr><td><b>{{ r[0] }}</b></td><td>{{ r[1] }}</td><td>{{ r[2] }}</td><td>{{ r[3] }}</td><td class=mute>{{ '有' if r[4] else '' }}</td><td class=mute style="max-width:300px;word-break:break-all">{{ r[5] }}</td><td>{{ r[6] }}</td></tr>{% endfor %}</table>{% endif %}
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
<tr><td>数据库</td><td>{{ dbinfo }}</td></tr><tr><td>数据目录</td><td>{{ data_dir }}</td></tr><tr><td>登录保护</td><td>{{ '已开启（ADMIN_PASSWORD）' if auth_on else '未开启——部署到公网前请设置 ADMIN_PASSWORD' }}</td></tr>
<tr><td>规则文件</td><td>config/rules.yaml（分类）、config/packages.yaml（封装映射）、config/manufacturers.yaml（URL 模板）——改完提交到 GitHub 重新部署即可生效</td></tr></table></div></div>
{% endblock %}"""

env = Environment(loader=DictLoader({"base": BASE, "home": HOME, "jobs": JOBS, "parts": PARTS, "part": PART, "review": REVIEW,
                                     "categories": CATEGORIES, "library": LIBRARY, "export": EXPORT, "settings": SETTINGS}), autoescape=True)
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
    return render("jobs", request, title="任务控制", nav="jobs", user=user, s=pipeline.summary(), cfg=settings.get_all(), job=job,
                  history=[h.to_dict() for h in manager.history], lib=_lib_stats(), sources=sources)


def _run_ingest(job, source, batch, max_batches, reset, incremental=False):
    from .sources import SampleSource
    src = settings.api_source() if source == "api" else SampleSource()
    return pipeline.ingest(src, batch=batch, max_batches=max_batches, reset=reset, job=job.id, incremental=incremental,
                           stop=job.should_stop, log=job.say, progress=lambda p: job.progress.update(p))


def _run_process(job, batch, max_batches, llm, urls):
    cfg = settings.apply_env()
    return pipeline.process(batch=batch, max_batches=max_batches, use_llm=llm, do_urls=urls, job=job.id,
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
            manager.start("process", lambda j: _run_process(j, int(f.get("batch") or 200), mb, bool(f.get("llm")), bool(f.get("urls"))),
                          {"batch": f.get("batch"), "max_batches": mb, "llm": bool(f.get("llm")), "urls": bool(f.get("urls"))})
        elif kind == "scan":
            if not f.get("path"):
                return back("/jobs", "请填写仓库地址或目录", True)
            manager.start("scan", lambda j: _run_scan(j, f["path"].strip(), f.get("source_kind", "community")), {"path": f["path"], "kind": f.get("source_kind")})
        elif kind == "export":
            manager.start("export", lambda j: _run_export(j, False), {"apply": False})
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
    return back("/jobs", f"已重置 {n} 条为未处理")


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
    anynew = or_(StagedPart.new_category_id.isnot(None), StagedPart.new_symbol.isnot(None), StagedPart.new_footprint.isnot(None), StagedPart.new_url.isnot(None), StagedPart.new_datasheet.isnot(None))
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
    return render("part", request, title=p.mpn, nav="parts", user=user, p=p, props=props, tax=tax, leaves=tax.leaf_labels())


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
    for k in ("new_symbol", "new_footprint", "new_url", "new_datasheet"):
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


@app.post("/review/{rid}/{decision}")
def review_do(rid: int, decision: str, next: str = "", user: str = Depends(auth)):
    if decision not in ("approve", "reject", "revert"):
        raise HTTPException(400)
    pr = pipeline.review(rid, decision, user=user)
    return RedirectResponse(next or f"/parts/{pr.part_id}", status_code=303)


# ---- 分类 / 库
@app.get("/categories", response_class=HTMLResponse)
def categories(request: Request, user: str = Depends(auth)):
    db = get_session()
    tax = pipeline.load_taxonomy(db)
    counts = dict(db.execute(select(StagedPart.orig_category_id, func.count()).group_by(StagedPart.orig_category_id)).all())
    ncounts = dict(db.execute(select(StagedPart.new_category_id, func.count()).where(StagedPart.new_category_id.isnot(None)).group_by(StagedPart.new_category_id)).all())

    def build(nid):
        n = tax.nodes[nid]
        return {"name": n.name, "id": n.id, "children": [build(c) for c in n.children]}
    roots = [build(n.id) for n in tax.nodes.values() if not n.parent]
    return render("categories", request, title="分类", nav="categories", user=user, roots=roots, counts=counts, ncounts=ncounts)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", kind: str = "symbols", user: str = Depends(auth)):
    rows, stats = [], {"symbols": 0, "footprints": 0, "instances": 0}
    if settings.LIBINDEX_PATH.exists():
        con = sqlite3.connect(str(settings.LIBINDEX_PATH))
        stats = _lib_stats()
        like = f"%{q}%"
        if kind == "symbols":
            rows = con.execute("select name, lib, pin_count, footprint_field, mpn_field, source, source_kind, license from symbols where name like ? or lib like ? or mpn_field like ? order by name limit 300", (like, like, like)).fetchall()
        else:
            rows = con.execute("select name, lib, pad_count, attr, model3d, source, source_kind from footprints where name like ? or lib like ? order by name limit 300", (like, like)).fetchall()
    return render("library", request, title="符号 / 封装库", nav="library", user=user, rows=rows, q=q, kind=kind, stats=stats)


# ---- 回写
def _export_ctx():
    db = get_session()
    s = pipeline.summary()
    anynew = or_(StagedPart.new_category_id.isnot(None), StagedPart.new_symbol.isnot(None), StagedPart.new_footprint.isnot(None), StagedPart.new_url.isnot(None), StagedPart.new_datasheet.isnot(None))
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
                  test=request.query_params.get("test", ""), dbinfo=dbinfo, data_dir=str(settings.DATA_DIR), auth_on=bool(os.environ.get("ADMIN_PASSWORD") or os.environ.get("EZPLM_WEB_PASSWORD")))


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
