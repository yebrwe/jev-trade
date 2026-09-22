"""Local dashboard: reads the bot's log files and serves a small page on localhost.

No exchange connection. Everything shown comes from LOG_DIR:
  decisions.jsonl   every cycle: equity, price, position, judgment, decision, execution
  trades.jsonl      closed trades (written by bot.py)
  runtime_state.json, monitor_state.json, perspective.json, news_checks.jsonl

    python -m jev_trade dashboard --port 8787   ->  http://127.0.0.1:8787
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Settings
from .timeframes import tf_seconds


def _read_jsonl(path: Path, last: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-last:] if last else rows


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def build_state(s: Settings) -> dict:
    log = s.log_dir
    # only records from the same execution mode (paper vs live/testnet) so equity series are comparable
    decisions = [d for d in _read_jsonl(log / "decisions.jsonl")
                 if d.get("judgment") and d.get("dry_run") == s.dry_run]
    trades = _read_jsonl(log / "trades.jsonl")
    runtime = _read_json(log / "runtime_state.json")
    monitor = _read_json(log / "monitor_state.json")
    perspective = _read_json(log / "perspective.json")
    checks = _read_jsonl(log / "news_checks.jsonl", last=1)
    now = time.time()
    tf_sec = tf_seconds(s.decision_timeframe)

    mode = "PAPER" if s.dry_run else ("TESTNET" if s.binance_testnet else "LIVE")
    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode, "symbol": s.symbol, "decision_timeframe": s.decision_timeframe,
        "bot_alive": False, "last_cycle": None, "last_cycle_age_sec": None,
    }
    if not decisions:
        out["note"] = "decisions.jsonl 에 기록이 없습니다. 봇을 먼저 실행하세요."
        return out

    last = decisions[-1]
    age = now - _ts(last["time"])
    out.update(last_cycle=last["time"], last_cycle_age_sec=int(age), bot_alive=age < 2.5 * tf_sec,
               price=last.get("price"))

    # ---- position: after the last cycle's execution ----
    pos = last.get("position_before")
    ex = last.get("execution") or {}
    dec = last.get("decision") or {}
    if ex.get("opened"):
        pos = {"side": ex["opened"], "qty": ex.get("qty"), "entry_price": last.get("price"),
               "leverage": ex.get("leverage") or dec.get("leverage"), "stop_loss": ex.get("stop_loss"),
               "take_profit": ex.get("take_profit"), "opened_at": ex.get("opened_at"),
               "unrealized_pnl_pct": 0.0, "mark_price": last.get("price"), "liquidation_price": None}
    elif ex.get("closed"):
        pos = None
    position = None
    if pos:
        mark = pos.get("mark_price") or last.get("price")
        notional = (pos.get("qty") or 0) * (mark or 0)
        upnl_pct = pos.get("unrealized_pnl_pct")
        lev = pos.get("leverage") or 1
        position = {
            "side": pos.get("side"), "qty": pos.get("qty"), "entry_price": pos.get("entry_price"),
            "mark_price": mark, "leverage": lev, "notional": notional, "margin": notional / lev if lev else None,
            "unrealized_pnl_pct": upnl_pct,
            "unrealized_pnl_usd": (notional * upnl_pct / 100) if upnl_pct is not None else None,
            "roe_pct": (upnl_pct * lev) if upnl_pct is not None else None,
            "stop_loss": pos.get("stop_loss"), "take_profit": pos.get("take_profit"),
            "liquidation_price": pos.get("liquidation_price"),
            "opened_at": pos.get("opened_at") or runtime.get("position_opened_at"),
            "held_min": int((now - (pos.get("opened_at") or runtime.get("position_opened_at") or now)) / 60),
        }
    out["position"] = position

    # ---- equity series (wallet equity + unrealized of the position at that moment) ----
    series = []
    for d in decisions:
        eq = d.get("equity")
        if eq is None:
            continue
        pb = d.get("position_before") or {}
        upnl = 0.0
        if pb and pb.get("unrealized_pnl_pct") is not None:
            upnl = (pb.get("qty") or 0) * (pb.get("mark_price") or d.get("price") or 0) * pb["unrealized_pnl_pct"] / 100
        series.append({"t": d["time"], "equity": eq, "total": eq + upnl, "price": d.get("price")})
    out["equity_series"] = series[-600:]
    equity_now = series[-1]["equity"] if series else None
    total_now = (equity_now or 0) + (position["unrealized_pnl_usd"] or 0 if position else 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_rows = [x for x in series if x["t"].startswith(today)]
    day_start = day_rows[0]["equity"] if day_rows else (series[0]["equity"] if series else None)
    first = series[0]["equity"] if series else None
    out["equity"] = {
        "wallet": equity_now, "total": total_now,
        "day_start": day_start,
        "today_pnl": (total_now - day_start) if day_start is not None else None,
        "today_pnl_pct": ((total_now / day_start - 1) * 100) if day_start else None,
        "since_start_pnl": (total_now - first) if first is not None else None,
        "since_start_pnl_pct": ((total_now / first - 1) * 100) if first else None,
        "peak": max(x["total"] for x in series) if series else None,
    }
    if series:
        peak = max(x["total"] for x in series)
        out["equity"]["drawdown_pct"] = (total_now / peak - 1) * 100 if peak else None

    # ---- trades ----
    today_trades = [t for t in trades if str(t.get("time", "")).startswith(today)]
    wins = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
    out["trades"] = {
        "total": len(trades), "today": len(today_trades),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "pnl_total": sum(t.get("pnl_usd") or 0 for t in trades),
        "pnl_today": sum(t.get("pnl_usd") or 0 for t in today_trades),
        "recent": list(reversed(trades[-20:])),
    }

    # ---- recent decisions ----
    rec = []
    for d in reversed(decisions[-40:]):
        j, dd = d["judgment"], d.get("decision") or {}
        rec.append({
            "time": d["time"], "price": d.get("price"),
            "entry": j.get("entry_action"), "probs": j.get("entry_probs"),
            "setup": j.get("setup_score"), "conviction": j.get("conviction_score"),
            "choppy": j.get("choppy"), "overbought": j.get("overbought"), "oversold": j.get("oversold"),
            "position_action": j.get("position_action"),
            "p_exit": (j.get("position_probs") or {}).get("exit"),
            "action": dd.get("action"), "leverage": dd.get("leverage"), "risk_pct": dd.get("risk_pct"),
            "reason": (dd.get("reasons") or [""])[0],
            "executed": bool(d.get("execution")),
        })
    out["decisions"] = rec
    out["action_counts"] = {}
    for d in decisions:
        a = (d.get("decision") or {}).get("action")
        out["action_counts"][a] = out["action_counts"].get(a, 0) + 1

    # ---- perspective / monitor / runtime ----
    p = perspective.get("perspective") or {}
    out["perspective"] = {
        "present": bool(p), "generated_at_utc": perspective.get("generated_at_utc"), "kind": perspective.get("kind"),
        "macro_bias": p.get("macro_bias"), "risk_appetite": p.get("risk_appetite"),
        "event_risk_level": p.get("event_risk_level"), "trading_stance": p.get("trading_stance"),
        "today_view": p.get("today_view"), "events": [f"{e.get('time_utc')}: {e.get('event')}" for e in (p.get("scheduled_events") or [])[:4]],
    }
    shock_until = float(monitor.get("shock_active_until") or 0)
    out["monitor"] = {
        "last_check": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(monitor["last_check"])) if monitor.get("last_check") else None,
        "shock_active": now < shock_until,
        "shock_until": time.strftime("%H:%M UTC", time.gmtime(shock_until)) if now < shock_until else None,
        "last_result": checks[0] if checks else None,
    }
    cooldown = s.cooldown_candles * tf_sec
    last_exit = runtime.get("last_exit_at")
    opens_today = sum(1 for d in decisions if d["time"].startswith(today) and (d.get("execution") or {}).get("opened"))
    out["runtime"] = {
        "trades_today": max(opens_today, runtime.get("trades_today", 0) if runtime.get("trades_day") == today else 0),
        "max_trades_per_day": s.max_trades_per_day,
        "cooldown_remaining_sec": int(max(0, cooldown - (now - last_exit))) if last_exit else 0,
    }
    out["settings"] = {
        "leverage_tiers": s.leverage_tiers, "risk_tiers": s.risk_tiers, "atr_stop_mult": s.atr_stop_mult,
        "atr_tp_mult": s.atr_tp_mult, "min_entry_prob": s.min_entry_prob, "min_direction_edge": s.min_direction_edge,
        "min_setup_score": s.min_setup_score, "choppy_max": s.choppy_max, "overextended_max": s.overextended_max,
    }
    return out


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>jev_trade 대시보드</title>
<style>
:root{color-scheme:light;--bg:#f4f4f2;--surface:#fcfcfb;--ink:#1c1f26;--muted:#5f6675;--line:#dcdcd6;--series:#2a78d6;--good:#0ca30c;--bad:#d03b3b;--warn:#fab219;--jev:#b8741a;--jev-soft:#fbf1e0;--claude:#2b6f74;--claude-soft:#e3f0f0}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#131416;--surface:#1a1a19;--ink:#e8e6df;--muted:#a2a8b4;--line:#33352f;--series:#5f9be6;--good:#3fc23f;--bad:#e06060;--warn:#fab219;--jev:#e0a24a;--jev-soft:#3a2d17;--claude:#6fc0c4;--claude-soft:#17363a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px 18px;margin-bottom:14px}
h1{font-size:20px;margin:0}.pill{font:12px/1 "IBM Plex Mono",monospace;border:1px solid var(--line);border-radius:999px;padding:6px 10px;background:var(--surface)}
.pill.live{border-color:var(--good)}.pill.dead{border-color:var(--bad)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.grid{display:grid;gap:12px}.kpis{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.tile .l{font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}.tile .s{font-size:12px;color:var(--muted)}
.pos{color:var(--good)}.neg{color:var(--bad)}
.cols{grid-template-columns:2fr 1fr}@media(max-width:820px){.cols{grid-template-columns:1fr}}
section{margin-top:18px}h2{font-size:15px;margin:0 0 8px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{padding:6px 8px;border-top:1px solid var(--line);text-align:left;white-space:nowrap;font-variant-numeric:tabular-nums}th{color:var(--muted);font-weight:500;border-top:0;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
.tbl{overflow-x:auto}.tag{display:inline-block;padding:1px 7px;border-radius:4px;font:11.5px "IBM Plex Mono",monospace}
.tag.long{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--good)}.tag.short{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}.tag.hold{background:var(--line);color:var(--muted)}.tag.exit{background:color-mix(in srgb,var(--warn) 30%,transparent);color:var(--ink)}
.bar{display:inline-block;height:8px;border-radius:2px;vertical-align:middle}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px}.kv b{color:var(--muted);font-weight:500}
svg{display:block;width:100%;height:auto}.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:12px "IBM Plex Mono",monospace;display:none}
.chartwrap{position:relative}.muted{color:var(--muted)}.small{font-size:12px}
</style></head><body><div class="wrap">
<header><h1>jev_trade 대시보드</h1><span id="mode" class="pill"></span><span id="alive" class="pill"></span><span id="sym" class="pill"></span><span id="upd" class="muted small"></span></header>
<div class="grid kpis" id="kpis"></div>
<section class="grid cols">
  <div class="card"><h2>자산 곡선 (지갑 잔고 + 미실현)</h2><div class="chartwrap"><div id="chart"></div><div class="tip" id="tip"></div></div></div>
  <div class="card"><h2>현재 포지션</h2><div id="position"></div><h2 style="margin-top:14px">관점 · 뉴스 감시</h2><div id="ctx"></div></div>
</section>
<section class="card"><h2>최근 판단</h2><div class="tbl"><table><thead><tr><th>시각(UTC)</th><th>가격</th><th>Jev 진입</th><th>롱 / 홀드 / 숏</th><th>setup</th><th>확신</th><th>포지션</th><th>행동</th><th>배수·리스크</th><th>사유</th></tr></thead><tbody id="dec"></tbody></table></div></section>
<section class="card"><h2>체결된 거래</h2><div class="tbl"><table><thead><tr><th>청산 시각(UTC)</th><th>방향</th><th>수량</th><th>진입</th><th>청산</th><th>배수</th><th>손익 (USDT)</th><th>사유</th></tr></thead><tbody id="trades"></tbody></table></div></section>
<section class="card small muted" id="settings"></section>
</div>
<script>
const $=id=>document.getElementById(id);const f=(x,d=2)=>x==null?'–':Number(x).toLocaleString('en-US',{maximumFractionDigits:d,minimumFractionDigits:d});
const pct=x=>x==null?'–':(x>=0?'+':'')+Number(x).toFixed(2)+'%';const cls=x=>x==null?'':(x>=0?'pos':'neg');
const tag=a=>`<span class="tag ${a||'hold'}">${(a||'hold').toUpperCase()}</span>`;
function bars(p){if(!p)return'';const L=p.long||0,H=p.hold||0,S=p.short||0;return `<span class="bar" style="width:${L*60}px;background:var(--good)"></span><span class="bar" style="width:${H*60}px;background:var(--line)"></span><span class="bar" style="width:${S*60}px;background:var(--bad)"></span> <span class="muted">${(L*100)|0}/${(H*100)|0}/${(S*100)|0}</span>`}
function chart(series){const W=720,H=240,P={l:56,r:12,t:12,b:26};const el=$('chart');if(!series||series.length<2){el.innerHTML='<p class="muted">데이터가 2개 이상 쌓이면 그려집니다.</p>';return}
const ys=series.map(s=>s.total),mn=Math.min(...ys),mx=Math.max(...ys),pad=(mx-mn||1)*0.15,y0=mn-pad,y1=mx+pad;const x=i=>P.l+(W-P.l-P.r)*i/(series.length-1),y=v=>P.t+(H-P.t-P.b)*(1-(v-y0)/(y1-y0));
let g='';for(let k=0;k<=4;k++){const v=y0+(y1-y0)*k/4,yy=y(v);g+=`<line x1="${P.l}" x2="${W-P.r}" y1="${yy}" y2="${yy}" stroke="var(--line)" stroke-width="1"/><text x="${P.l-6}" y="${yy+4}" text-anchor="end" font-size="11" fill="var(--muted)">${f(v,0)}</text>`}
const pts=series.map((s,i)=>`${x(i)},${y(s.total)}`).join(' ');const area=`M${x(0)},${y(y0)} L`+pts+` L${x(series.length-1)},${y(y0)} Z`;
const t0=series[0].t.slice(5,16).replace('T',' '),t1=series[series.length-1].t.slice(5,16).replace('T',' ');
el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="자산 곡선"><path d="${area}" fill="var(--series)" opacity="0.10"/><polyline points="${pts}" fill="none" stroke="var(--series)" stroke-width="2" stroke-linejoin="round"/>${g}<circle cx="${x(series.length-1)}" cy="${y(series[series.length-1].total)}" r="4" fill="var(--series)" stroke="var(--surface)" stroke-width="2"/><text x="${P.l}" y="${H-8}" font-size="11" fill="var(--muted)">${t0}</text><text x="${W-P.r}" y="${H-8}" font-size="11" text-anchor="end" fill="var(--muted)">${t1}</text><line id="xh" x1="0" x2="0" y1="${P.t}" y2="${H-P.b}" stroke="var(--muted)" stroke-dasharray="3 3" style="display:none"/></svg>`;
const svg=el.querySelector('svg'),tip=$('tip'),xh=el.querySelector('#xh');svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect();const px=(e.clientX-r.left)/r.width*W;const i=Math.max(0,Math.min(series.length-1,Math.round((px-P.l)/(W-P.l-P.r)*(series.length-1))));const s=series[i];xh.setAttribute('x1',x(i));xh.setAttribute('x2',x(i));xh.style.display='';tip.style.display='block';tip.style.left=(e.clientX-r.left+12)+'px';tip.style.top=(e.clientY-r.top-10)+'px';tip.innerHTML=`${s.t.slice(0,16).replace('T',' ')}<br>총자산 ${f(s.total)}<br>지갑 ${f(s.equity)}<br>BTC ${f(s.price,1)}`});svg.addEventListener('mouseleave',()=>{tip.style.display='none';xh.style.display='none'})}
async function load(){const r=await fetch('/api/state');const d=await r.json();
$('mode').textContent=d.mode+' · '+d.decision_timeframe;$('sym').textContent=d.symbol;$('alive').innerHTML=`<span class="dot" style="background:${d.bot_alive?'var(--good)':'var(--bad)'}"></span>${d.bot_alive?'봇 동작 중':'봇 응답 없음'}${d.last_cycle_age_sec!=null?' · 마지막 판단 '+Math.round(d.last_cycle_age_sec/60)+'분 전':''}`;$('alive').className='pill '+(d.bot_alive?'live':'dead');$('upd').textContent='갱신 '+new Date().toLocaleTimeString('ko-KR');
if(d.note){$('kpis').innerHTML=`<div class="tile">${d.note}</div>`;return}
const e=d.equity,t=d.trades;$('kpis').innerHTML=[['총자산 (USDT)',f(e.total),`지갑 ${f(e.wallet)}`],['오늘 손익',`<span class="${cls(e.today_pnl)}">${f(e.today_pnl)}</span>`,pct(e.today_pnl_pct)+' · UTC 기준'],['시작 이후 손익',`<span class="${cls(e.since_start_pnl)}">${f(e.since_start_pnl)}</span>`,pct(e.since_start_pnl_pct)],['고점 대비',`<span class="${cls(e.drawdown_pct)}">${pct(e.drawdown_pct)}</span>`,`고점 ${f(e.peak)}`],['거래',`${t.today} <span class="muted small">오늘</span> / ${t.total}`,`승률 ${t.win_rate==null?'–':(t.win_rate*100).toFixed(0)+'%'} · 실현 ${f(t.pnl_total)}`],['BTC 가격',f(d.price,1),`한도 ${d.runtime.trades_today}/${d.runtime.max_trades_per_day}${d.runtime.cooldown_remaining_sec?' · 쿨다운 '+Math.ceil(d.runtime.cooldown_remaining_sec/60)+'분':''}`]].map(k=>`<div class="tile"><div class="l">${k[0]}</div><div class="v">${k[1]}</div><div class="s">${k[2]}</div></div>`).join('');
chart(d.equity_series);
const p=d.position;$('position').innerHTML=p?`<div class="kv"><b>방향</b><span>${tag(p.side)} ${f(p.qty,4)} BTC · 명목 ${f(p.notional,0)}</span><b>진입 / 현재</b><span>${f(p.entry_price,1)} / ${f(p.mark_price,1)}</span><b>미실현</b><span class="${cls(p.unrealized_pnl_usd)}">${f(p.unrealized_pnl_usd)} USDT (${pct(p.unrealized_pnl_pct)} · ROE ${pct(p.roe_pct)})</span><b>배수 / 증거금</b><span>${p.leverage}x / ${f(p.margin,0)} USDT</span><b>손절 / 익절</b><span>${f(p.stop_loss,1)} / ${f(p.take_profit,1)}</span><b>청산가</b><span>${f(p.liquidation_price,1)}</span><b>보유</b><span>${p.held_min}분</span></div>`:'<p class="muted">포지션 없음</p>';
const ps=d.perspective,m=d.monitor;$('ctx').innerHTML=`<div class="kv">${ps.present?`<b>관점</b><span>${ps.macro_bias} · ${ps.risk_appetite} · risk ${ps.event_risk_level} · <b style="color:var(--claude)">${ps.trading_stance}</b> <span class="muted">(${ps.generated_at_utc} ${ps.kind})</span></span><b>오늘</b><span class="small">${ps.today_view||''}</span>${ps.events.length?`<b>이벤트</b><span class="small">${ps.events.join('<br>')}</span>`:''}`:'<b>관점</b><span class="muted">없음</span>'}<b>뉴스</b><span>${m.last_check||'–'}${m.last_result?` · 새 ${m.last_result.new_headlines}건, 중대 ${m.last_result.material.length}, 쇼크 ${m.last_result.shocks.length}`:''}${m.shock_active?` <span class="tag exit">쇼크 차단 ~${m.shock_until}</span>`:''}</span></div>`;
$('dec').innerHTML=d.decisions.map(x=>`<tr><td>${x.time.slice(5,16).replace('T',' ')}</td><td>${f(x.price,1)}</td><td>${tag(x.entry)}</td><td>${bars(x.probs)}</td><td>${f(x.setup,2)}</td><td>${f(x.conviction,2)}</td><td>${x.position_action?x.position_action+' (exit '+f(x.p_exit,2)+')':'–'}</td><td>${tag(x.action)}${x.executed?' ✓':''}</td><td>${x.leverage?x.leverage+'x · '+x.risk_pct+'%':'–'}</td><td class="muted" style="white-space:normal;min-width:260px">${x.reason}</td></tr>`).join('');
$('trades').innerHTML=t.recent.length?t.recent.map(x=>`<tr><td>${(x.time||'').slice(5,16).replace('T',' ')}</td><td>${tag(x.side)}</td><td>${f(x.qty,4)}</td><td>${f(x.entry_price,1)}</td><td>${f(x.exit_price,1)}</td><td>${x.leverage?x.leverage+'x':'–'}</td><td class="${cls(x.pnl_usd)}">${f(x.pnl_usd)}${x.estimated?' <span class="muted">(추정)</span>':''}</td><td class="muted">${x.reason||''}</td></tr>`).join(''):'<tr><td colspan="8" class="muted">아직 청산된 거래가 없습니다</td></tr>';
const s=d.settings;$('settings').textContent=`설정: 배수 tier ${s.leverage_tiers.join('/')}x · 리스크 tier ${s.risk_tiers.join('/')}% · 손절 ATR×${s.atr_stop_mult} · 익절 ATR×${s.atr_tp_mult} · 진입 P≥${s.min_entry_prob} 격차≥${s.min_direction_edge} setup≥${s.min_setup_score} choppy<${s.choppy_max} 과열<${s.overextended_max} · 판단 분포 ${JSON.stringify(d.action_counts)}`;}
load();setInterval(load,10000);
</script></body></html>"""


def serve(settings: Settings, port: int = 8787, host: str = "127.0.0.1") -> None:
    s = settings

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/state"):
                body = json.dumps(build_state(s), ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
            else:
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # quiet
            pass

    httpd = ThreadingHTTPServer((host, port), H)
    print(f"[dashboard] http://{host}:{port}  (reads {s.log_dir.resolve()}, refreshes every 10s, Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
