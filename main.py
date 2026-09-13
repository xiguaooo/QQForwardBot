from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from config import ConfigManager
from ws_client import HELP_TEXT, RuntimeState, SnowLumaForwarder


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

config_manager = ConfigManager()
state = RuntimeState()
forwarder = SnowLumaForwarder(config_manager, state)


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str | None = None
    forward_mode: str | None = None
    qz_action: str | None = None
    snowluma_ws_url: str | None = None
    web_port: int | str | None = None
    groups: list[dict[str, Any]] | None = None
    source_group_id: int | str | None = None
    target_group_id: int | str | None = None
    forward_template: str | None = None
    batch_size: int | str | None = None
    watched_qq_ids: list[str] | str | None = None
    qz_poll_interval: int | str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(forwarder.run_forever(), name="snowluma-forwarder")
    cfg = config_manager.get()
    if not any(group.source_group_id and group.target_group_ids for group in cfg.groups):
        state.log("warning", "当前没有可用的分组源群/目标群配置")
    try:
        yield
    finally:
        await forwarder.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="OneBot 协议端", version="2.1.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        **state.snapshot(),
        "render_mode": "per-group",
        "commands": {
            "help": "/help",
            "ms": "/ms <count>",
            "qz": "/qz <count>",
            "status": "/status",
        },
        "config": config_manager.get().to_dict(),
    }


@app.get("/api/logs")
async def api_logs() -> dict[str, Any]:
    return {"logs": list(state.logs), "forward_records": list(state.forward_records)}


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    return config_manager.get().to_dict()


@app.post("/api/config")
async def api_update_config(body: ConfigUpdate) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    try:
        old, new = config_manager.update(patch)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reconnect_required = old.snowluma_ws_url != new.snowluma_ws_url or old.backend != new.backend
    restart_required = old.web_port != new.web_port
    state.log("info", "配置已保存到 config.json")
    if reconnect_required:
        await forwarder.force_reconnect()
    return {
        "ok": True,
        "config": new.to_dict(),
        "reconnected": reconnect_required,
        "restart_required": restart_required,
        "message": "web_port 修改后需重启程序生效" if restart_required else "配置已生效",
    }


@app.post("/api/reconnect")
async def api_reconnect() -> dict[str, bool]:
    await forwarder.force_reconnect()
    return {"ok": True}


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OneBot 协议端</title>
  <style>
    :root { color-scheme:light; --bg:#eaf5ff; --panel:rgba(255,255,255,.72); --panel-strong:rgba(255,255,255,.88); --text:#16324d; --muted:#6d8499; --line:rgba(91,142,177,.22); --accent:#1599e8; --accent-deep:#087bc5; --ok:#1da879; --bad:#df6170; --warn:#c78727; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; color:var(--text); font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; background-color:var(--bg); background-image:linear-gradient(135deg,rgba(255,255,255,.56),rgba(207,234,255,.35)); }
    button,input,textarea,select { font:inherit; }
    button { cursor:pointer; }
    .wrap { width:min(1180px,calc(100% - 36px)); margin:0 auto; padding:30px 0 60px; }
    .topbar { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:26px; }
    .brand { display:flex; align-items:center; gap:14px; }
    .brand-mark { display:grid; place-items:center; width:46px; height:46px; border-radius:14px; color:white; font-size:23px; font-weight:700; background:linear-gradient(145deg,#3ecbfa,#168be4); box-shadow:0 10px 24px rgba(26,143,222,.2); }
    .eyebrow,.section-kicker { margin:0 0 5px; color:#4e91b8; font-size:11px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
    h1,h2,h3,p { margin-top:0; } h1 { margin-bottom:4px; font-size:30px; letter-spacing:0; } h2 { margin-bottom:4px; font-size:19px; } h3 { margin:0; font-size:15px; }
    .subtitle,.muted,.section-note { color:var(--muted); } .subtitle { margin:0; font-size:13px; }
    .status-pill { display:inline-flex; align-items:center; gap:9px; padding:10px 14px; color:#517087; border:1px solid var(--line); border-radius:999px; background:rgba(255,255,255,.66); box-shadow:0 8px 22px rgba(70,125,160,.08); white-space:nowrap; }
    .dot { width:9px; height:9px; border-radius:50%; background:var(--bad); box-shadow:0 0 0 4px rgba(223,97,112,.11); } .dot.ok { background:var(--ok); box-shadow:0 0 0 4px rgba(29,168,121,.12); }
    .stack { display:grid; gap:18px; }
    .panel { min-width:0; padding:22px; border:1px solid rgba(255,255,255,.75); border-radius:18px; background:var(--panel); box-shadow:0 14px 36px rgba(63,119,153,.1); backdrop-filter:blur(18px); }
    .section-head,.group-head,.actions,.command-row { display:flex; align-items:center; justify-content:space-between; gap:14px; }
    .section-note { margin:0; font-size:13px; line-height:1.55; }
    .overview-head { display:flex; align-items:end; justify-content:space-between; gap:20px; margin-bottom:18px; }
    .live-readout { color:var(--accent-deep); font-size:12px; font-weight:700; }
    .stats { display:grid; grid-template-columns:repeat(5,1fr); border-top:1px solid var(--line); }
    .stat { min-width:0; padding:16px 14px 3px; border-right:1px solid var(--line); } .stat:last-child { border-right:0; } .stat-label { display:block; color:var(--muted); font-size:12px; } .stat b { display:block; margin-top:7px; color:var(--text); font-size:24px; line-height:1; }
    .stat b.online { color:var(--ok); } .stat b.offline { color:var(--bad); }
    .config-grid { display:grid; grid-template-columns:1.08fr .92fr; gap:18px; } .field-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; } .field { min-width:0; }
    label { display:block; margin:0 0 7px; color:#59738a; font-size:12px; font-weight:700; }
    input,textarea,select { width:100%; color:var(--text); border:1px solid var(--line); border-radius:10px; outline:none; background:rgba(255,255,255,.72); transition:border-color .18s,box-shadow .18s,background .18s; }
    input,select { height:40px; padding:0 12px; } textarea { min-height:100px; padding:11px 12px; resize:vertical; line-height:1.55; } input:focus,textarea:focus,select:focus { border-color:rgba(21,153,232,.7); background:#fff; box-shadow:0 0 0 3px rgba(21,153,232,.13); }
    .token-note { display:flex; align-items:center; min-height:40px; padding:0 12px; color:var(--muted); border:1px dashed rgba(91,142,177,.32); border-radius:10px; background:rgba(231,245,255,.54); font-size:12px; }
    .button { display:inline-flex; align-items:center; justify-content:center; gap:7px; min-height:38px; padding:0 13px; color:#47718c; border:1px solid var(--line); border-radius:10px; background:rgba(255,255,255,.66); transition:transform .18s,box-shadow .18s,background .18s; }
    .button:hover { transform:translateY(-1px); background:#fff; box-shadow:0 7px 18px rgba(59,117,152,.12); } .button.primary { color:white; border-color:transparent; background:linear-gradient(135deg,#2dbcf1,#128bdf); box-shadow:0 8px 18px rgba(18,139,223,.2); } .button.danger { color:#c05967; background:rgba(255,242,244,.7); } .button-icon { font-size:17px; line-height:1; }
    .actions { justify-content:flex-start; margin-top:16px; } .notice { min-height:20px; margin:12px 0 0; color:var(--muted); font-size:12px; }
    .group-list { display:grid; gap:14px; margin-top:16px; } .group-editor { padding:17px; border:1px solid rgba(91,142,177,.2); border-radius:14px; background:rgba(255,255,255,.56); } .group-head { margin-bottom:15px; } .group-title { display:flex; align-items:center; gap:9px; font-size:15px; font-weight:800; } .group-index { display:grid; place-items:center; width:25px; height:25px; color:white; border-radius:8px; background:#4ab5e8; font-size:11px; }
    .group-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:11px; } .group-grid .wide { grid-column:span 2; } .group-template { margin-top:13px; }
    .command-row { justify-content:flex-start; flex-wrap:wrap; padding-top:15px; } .command-label { margin-right:2px; color:var(--muted); font-size:13px; } .command-chip { display:inline-flex; align-items:center; min-height:30px; padding:0 10px; color:#287bad; border:1px solid rgba(53,157,209,.2); border-radius:8px; background:rgba(225,246,255,.75); font:700 12px/1 Consolas,monospace; }
    .logbox { height:260px; overflow:auto; padding:11px 13px; border:1px solid rgba(72,125,158,.18); border-radius:12px; background:rgba(236,247,255,.75); font:12px/1.7 Consolas,"Cascadia Mono",monospace; } .logline { padding:3px 0; border-bottom:1px solid rgba(91,142,177,.12); white-space:pre-wrap; word-break:break-word; } .INFO { color:#3a718f; } .WARNING { color:var(--warn); } .ERROR { color:var(--bad); }
    table { width:100%; border-collapse:collapse; font-size:12px; } th,td { padding:10px 8px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); } th { color:var(--muted); font-weight:700; } .scroll { min-width:0; max-height:320px; overflow:auto; }
    @media (max-width:980px) { .config-grid { grid-template-columns:1fr; } .group-grid { grid-template-columns:1fr 1fr; } }
    @media (max-width:700px) { .wrap { width:min(100% - 24px,560px); padding-top:18px; } .topbar,.overview-head { align-items:flex-start; flex-direction:column; } h1 { font-size:26px; } .status-pill { align-self:stretch; justify-content:center; } .panel { padding:17px; border-radius:15px; } .stats { grid-template-columns:repeat(2,1fr); } .stat { border-bottom:1px solid var(--line); } .stat:nth-child(2n) { border-right:0; } .stat:last-child { grid-column:span 2; } }
    @media (max-width:480px) { .field-grid,.group-grid { grid-template-columns:1fr; } .group-grid .wide { grid-column:auto; } .actions .button { flex:1; } table { min-width:620px; } }
  </style>
</head>
<body>
<div class="wrap">
  <header class="topbar"><div class="brand"><div class="brand-mark">↗</div><div><p class="eyebrow">OneBot / go-cqhttp / NapCat</p><h1>OneBot 协议端</h1><p class="subtitle">消息中转 · 原生合并转发 / QQ8 风格长图</p></div></div><div class="status-pill"><span id="dot" class="dot"></span><span id="connText">正在读取状态…</span></div></header>
  <main class="stack">
    <section class="panel"><div class="overview-head"><div><p class="section-kicker">Runtime</p><h2>运行状态</h2><p class="section-note" id="lastError">最近错误：无</p></div><span class="live-readout">每 1.5 秒同步</span></div><div class="stats"><div class="stat"><span class="stat-label">连接</span><b id="connectedStat">--</b></div><div class="stat"><span class="stat-label">活动分组</span><b id="groupStat">0</b></div><div class="stat"><span class="stat-label">待转发</span><b id="pendingStat">0</b></div><div class="stat"><span class="stat-label">动态推送</span><b id="qzStat">0</b></div><div class="stat"><span class="stat-label">发送记录</span><b id="forwardStat">0</b></div></div><div class="actions"><button class="button" title="重新连接 OneBot WebSocket" onclick="manualReconnect()"><span class="button-icon">↻</span>重新连接</button><button class="button" title="刷新状态和日志" onclick="refreshAll()"><span class="button-icon">⟳</span>刷新</button></div></section>
    <section class="panel"><div class="section-head"><div><p class="section-kicker">Connection</p><h2>QQ 后端连接</h2><p class="section-note">连接正向 WebSocket 通用端点（go-cqhttp 使用 /，不要使用 /api 或 /event）。</p></div></div><div class="config-grid"><div><label for="backend">QQ 后端</label><select id="backend"><option value="auto">自动识别</option><option value="go-cqhttp">go-cqhttp</option><option value="napcat">NapCat</option></select></div><div><label for="wsUrl">WebSocket 地址</label><input id="wsUrl" placeholder="ws://127.0.0.1:8095" /></div><div><label for="webPort">Web 端口</label><input id="webPort" inputmode="numeric" /></div><div><label>访问令牌</label><div class="token-note">设置 ONEBOT_ACCESS_TOKEN（兼容 SNOWLUMA_ACCESS_TOKEN）</div></div><div class="actions"><button class="button primary" onclick="saveConfig()"><span class="button-icon">✓</span>保存全部配置</button></div></div><div id="configNotice" class="notice"></div></section>
    <section class="panel"><div class="section-head"><div><p class="section-kicker">Routing</p><h2>转发分组</h2><p class="section-note">每组独立管理一个源群、多个目标群、转发方式、批量阈值和动态监听。QQ 空间需要额外 SDK 或 bridge，go-cqhttp 不原生提供动态接口。</p></div><button class="button" onclick="addGroup()"><span class="button-icon">＋</span>添加分组</button></div><div id="groups" class="group-list"></div></section>
    <section class="panel"><div class="section-head"><div><p class="section-kicker">Commands</p><h2>目标群命令</h2></div></div><div class="command-row"><span class="command-label">可用命令</span><span class="command-chip">/help</span><span class="command-chip">/ms &lt;count&gt;</span><span class="command-chip">/qz &lt;count&gt;</span><span class="command-chip">/status</span></div></section>
    <section class="panel"><div class="section-head"><div><p class="section-kicker">Activity</p><h2>实时日志</h2></div><button class="button" title="刷新日志" onclick="loadLogs(true)"><span class="button-icon">↻</span>刷新日志</button></div><div id="logbox" class="logbox"></div></section>
    <section class="panel"><div class="section-head"><div><p class="section-kicker">History</p><h2>最近发送记录</h2></div></div><div class="scroll"><table><thead><tr><th>时间</th><th>分组 / 路由</th><th>内容</th><th>状态</th></tr></thead><tbody id="records"></tbody></table></div></section>
  </main>
</div>
<script>
  let configLoaded = false;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const defaultTemplate = '来自 {source_group_id} 的新消息：\n发送者：{sender_nickname}（{sender_id}）\n发送时间：{time}\n{message}';
  const blankGroup = index => ({id:`group-${index + 1}`, name:`分组 ${index + 1}`, source_group_id:0, target_group_ids:[], forward_template:defaultTemplate, batch_size:1, forward_mode:'image', watched_qq_ids:[], qz_poll_interval:30, qz_action:'get_emotion_list'});
  function field(label, cls, value, type='text', placeholder='') { return `<div class="field"><label>${label}</label><input class="${cls}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" /></div>`; }
  function renderGroups(groups) {
    const list = $('groups'); list.innerHTML = '';
  (groups || []).forEach((g, index) => {
    const card = document.createElement('div'); card.className = 'group-editor';
    card.innerHTML = `<div class="group-head"><div class="group-title"><span class="group-index">${String(index + 1).padStart(2,'0')}</span>分组 ${index + 1}</div><button class="button danger" title="移除这个分组" onclick="this.closest('.group-editor').remove()"><span class="button-icon">×</span>移除</button></div><div class="group-grid">${field('分组名称','g-name',g.name,'text','例如：主群转发')}${field('分组 ID','g-id',g.id,'text','group-main')}${field('源群号','g-source',g.source_group_id,'number','例如：源群号')}${field('目标群号（逗号分隔）','g-targets',(g.target_group_ids || []).join(','),'text','例如：目标群号, ...')}${field('普通消息阈值','g-batch',g.batch_size,'number','1')}<div class="field"><label>转发方式</label><select class="g-mode"><option value="image" ${g.forward_mode !== 'forward' ? 'selected' : ''}>QQ8 风格长图</option><option value="forward" ${g.forward_mode === 'forward' ? 'selected' : ''}>QQ 原生合并转发</option></select></div>${field('监听 QQ 动态（逗号分隔）','g-watch',(g.watched_qq_ids || []).join(','),'text','QQ 号，可留空')}${field('动态轮询秒数','g-interval',g.qz_poll_interval,'number','30')}${field('Qzone 动作','g-action',g.qz_action || 'get_emotion_list','text','get_emotion_list')}</div><div class="group-template"><label>本组转发模板</label><textarea class="g-template">${esc(g.forward_template || defaultTemplate)}</textarea></div>`;
    list.appendChild(card);
  });
  if (!(groups || []).length) list.innerHTML = '<p class="section-note">暂未创建分组，点击“添加分组”开始配置。</p>';
  }
  function collectGroups() {
    return [...document.querySelectorAll('.group-editor')].map((card, index) => ({
    id: card.querySelector('.g-id').value.trim() || `group-${index + 1}`,
    name: card.querySelector('.g-name').value.trim() || `分组 ${index + 1}`,
    source_group_id: card.querySelector('.g-source').value.trim(),
    target_group_ids: card.querySelector('.g-targets').value.split(',').map(x => x.trim()).filter(Boolean),
    batch_size: card.querySelector('.g-batch').value.trim() || '1',
    forward_mode: card.querySelector('.g-mode').value,
    watched_qq_ids: card.querySelector('.g-watch').value.split(',').map(x => x.trim()).filter(Boolean),
    qz_poll_interval: card.querySelector('.g-interval').value.trim() || '30',
    qz_action: card.querySelector('.g-action').value.trim() || 'get_emotion_list',
    forward_template: card.querySelector('.g-template').value
  }));
  }
  function addGroup() { const groups = collectGroups(); groups.push(blankGroup(groups.length)); renderGroups(groups); }
  function fillConfig(c) { if (!c) return; $('backend').value = c.backend || 'auto'; $('wsUrl').value = c.snowluma_ws_url ?? ''; $('webPort').value = c.web_port ?? 8000; renderGroups(c.groups || []); configLoaded = true; }
  async function loadStatus() { try { const r = await fetch('/api/status',{cache:'no-store'}); const d = await r.json(); $('dot').classList.toggle('ok',!!d.connected); $('connText').textContent = (d.backend && d.backend !== 'auto' ? d.backend : 'OneBot') + (d.connected ? ' 已连接' : ' 未连接'); $('connectedStat').textContent = d.connected ? '在线' : '离线'; $('connectedStat').className = d.connected ? 'online' : 'offline'; $('groupStat').textContent = (d.config?.groups || []).length; $('pendingStat').textContent = d.pending_batch_count ?? 0; $('qzStat').textContent = d.qz_forward_count ?? 0; $('forwardStat').textContent = d.forward_count ?? 0; $('lastError').textContent = '最近错误：' + (d.last_error || '无'); if (!configLoaded) fillConfig(d.config); } catch (e) { $('connText').textContent = '状态读取失败'; $('connectedStat').className = 'offline'; } }
async function loadLogs(force=false) { try { const box=$('logbox'); const stick=force || box.scrollHeight-box.scrollTop-box.clientHeight<60; const d=await (await fetch('/api/logs',{cache:'no-store'})).json(); box.innerHTML=(d.logs||[]).map(x=>`<div class="logline ${esc(x.level)}">[${esc(x.time)}] [${esc(x.level)}] ${esc(x.message)}</div>`).join(''); if(stick) box.scrollTop=box.scrollHeight; const records=[...(d.forward_records||[])].reverse(); $('records').innerHTML=records.map(x=>`<tr><td>${esc(x.time)}</td><td>${esc(x.route_name||'')}<br>${esc(x.source_group_id)} → ${esc(x.target_group_id)}</td><td>${esc(x.message)}</td><td>${esc(x.status)}${x.detail?'<br><span class="muted">'+esc(x.detail)+'</span>':''}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">暂无发送记录</td></tr>'; } catch(e) {} }
async function saveConfig() { const notice=$('configNotice'); notice.textContent='正在保存…'; try { const body={backend:$('backend').value,snowluma_ws_url:$('wsUrl').value.trim(),web_port:$('webPort').value.trim(),groups:collectGroups()}; const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const d=await r.json(); if(!r.ok) throw new Error(d.detail||'保存失败'); notice.textContent=d.message||'配置已保存'; configLoaded=false; fillConfig(d.config); await refreshAll(); } catch(e) { notice.textContent='保存失败：'+e.message; } }
async function manualReconnect() { try { await fetch('/api/reconnect',{method:'POST'}); setTimeout(refreshAll,500); } catch(e) {} }
async function refreshAll() { await Promise.all([loadStatus(),loadLogs(true)]); }
refreshAll(); setInterval(loadStatus,1500); setInterval(()=>loadLogs(false),1500);
</script>
</body>
</html>'''


if __name__ == "__main__":
    cfg = config_manager.get()
    uvicorn.run("main:app", host="127.0.0.1", port=cfg.web_port, reload=False, log_level="info")
