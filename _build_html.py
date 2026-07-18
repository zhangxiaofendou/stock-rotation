# -*- coding: utf-8 -*-
"""把 _report_data.json 内联进交互式 HTML 模板，输出 industry_state_review.html"""

with open("_report_data.json", "r", encoding="utf-8") as f:
    data_str = f.read()

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>行业状态验证 · 九宫格 x 250日K线</title>
<style>
  :root{
    --bg:#0e1117; --panel:#161b22; --panel2:#0d1117; --line:#30363d;
    --text:#e6edf3; --muted:#8b949e;
    --up:#ef4444; --down:#22c55e;
    --ma5:#3b82f6; --ma20:#a855f7; --ma60:#f59e0b;
    --accent:#38bdf8;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;height:100%;}
  body{background:var(--bg);color:var(--text);
    font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
    display:flex;flex-direction:column;height:100vh;overflow:hidden;}
  header{padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel);}
  header h1{font-size:16px;margin:0 0 6px;}
  .meta{font-size:12px;color:var(--muted);display:flex;gap:14px;flex-wrap:wrap;align-items:center;}
  .dist{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;}
  .chip{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);
    background:var(--panel2);cursor:pointer;user-select:none;transition:.12s;}
  .chip:hover{border-color:var(--accent);}
  .chip.active{background:var(--accent);color:#06283d;border-color:var(--accent);font-weight:600;}
  .chip .n{opacity:.7;margin-left:3px;}
  main{flex:1;display:flex;min-height:0;}
  .left{width:520px;flex:0 0 520px;border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;}
  .toolbar{padding:8px 12px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center;}
  .toolbar input{flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:6px;
    color:var(--text);padding:6px 10px;font-size:13px;outline:none;}
  .toolbar input:focus{border-color:var(--accent);}
  .toolbar select{background:var(--panel2);border:1px solid var(--line);border-radius:6px;
    color:var(--text);padding:6px 8px;font-size:12px;outline:none;}
  .tbl-wrap{flex:1;overflow-y:auto;min-height:0;}
  table{width:100%;border-collapse:collapse;font-size:12.5px;}
  thead th{position:sticky;top:0;background:var(--panel);z-index:1;text-align:right;
    padding:8px 10px;border-bottom:1px solid var(--line);color:var(--muted);
    font-weight:500;cursor:pointer;white-space:nowrap;}
  thead th.l{text-align:left;}
  thead th:hover{color:var(--accent);}
  tbody td{padding:7px 10px;border-bottom:1px solid #1c222b;text-align:right;white-space:nowrap;}
  tbody td.l{text-align:left;}
  tbody tr{cursor:pointer;}
  tbody tr:hover{background:#1c2430;}
  tbody tr.sel{background:#233043;}
  .sname{font-weight:600;}
  .scode{color:var(--muted);font-size:11px;}
  .tg{padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600;}
  .tg-up{background:rgba(239,68,68,.15);color:#f87171;}
  .tg-flat{background:rgba(139,148,158,.15);color:#c9d1d9;}
  .tg-down{background:rgba(34,197,94,.15);color:#4ade80;}
  .badge-neg{display:inline-block;margin-left:5px;padding:1px 6px;border-radius:9px;font-size:11px;font-weight:600;color:#16a34a;background:rgba(22,163,74,.14);border:1px solid rgba(22,163,74,.45);}
  .badge-pos{display:inline-block;margin-left:5px;padding:1px 6px;border-radius:9px;font-size:11px;font-weight:600;color:#e23c3c;background:rgba(226,60,60,.14);border:1px solid rgba(226,60,60,.45);}
  .st{font-weight:600;font-size:12px;}
  .pctbar{display:inline-block;width:34px;height:6px;border-radius:3px;background:#22303f;
    vertical-align:middle;margin-left:6px;position:relative;overflow:hidden;}
  .pctbar i{position:absolute;left:0;top:0;bottom:0;}
  .right{flex:1;display:flex;flex-direction:column;min-width:0;}
  .verdict{padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel);}
  .verdict .row1{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;}
  .verdict h2{font-size:16px;margin:0;}
  .verdict .why{font-size:12.5px;color:var(--muted);margin-top:8px;line-height:1.7;}
  .verdict .why b{color:var(--text);}
  .gate{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;margin:2px 4px 0 0;}
  .gate-ok{background:rgba(34,197,94,.15);color:#4ade80;}
  .gate-no{background:rgba(245,158,11,.18);color:#fbbf24;}
  .gate-na{background:rgba(139,148,158,.12);color:#8b949e;}
  .chart-area{flex:1;position:relative;min-height:0;padding:10px 14px 14px;}
  #chart{width:100%;height:100%;display:block;}
  .legend{position:absolute;top:16px;left:20px;font-size:11px;color:var(--muted);
    display:flex;gap:14px;pointer-events:none;}
  .legend span{display:flex;align-items:center;gap:4px;}
  .legend i{width:14px;height:3px;display:inline-block;border-radius:2px;}
  .empty{flex:1;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:14px;}
  .hint{font-size:11px;color:var(--muted);margin-left:auto;}
</style>
</head>
<body>
<header>
  <h1>行业状态验证 · 九宫格状态 x 最近250日K线</h1>
  <div class="meta">
    <span>截面日期：<b id="mDate" style="color:var(--text)"></b></span>
    <span>板块数：<b id="mCount" style="color:var(--text)"></b></span>
    <span style="color:var(--up)">阳线(涨)</span>
    <span style="color:var(--down)">阴线(跌)</span>
    <span class="hint">点击左侧任意行 -> 右侧查看该板块K线并核对状态</span>
  </div>
  <div class="dist" id="dist"></div>
</header>
<main>
  <div class="left">
    <div class="toolbar">
      <input id="search" placeholder="搜索板块名 / 代码...">
      <select id="fTrend">
        <option value="">全部趋势</option>
        <option value="上涨">上涨</option>
        <option value="横盘">横盘</option>
        <option value="下跌">下跌</option>
      </select>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th class="l" data-k="name">板块</th>
          <th data-k="trend">趋势</th>
          <th data-k="state">状态</th>
          <th data-k="rs_pct">RS分位</th>
          <th data-k="rs_mom_pct">动量分位</th>
          <th data-k="cross">横截面</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </div>
  <div class="right">
    <div class="verdict" id="verdict">
      <div class="empty" style="padding:30px 0;">选择一个板块开始验证</div>
    </div>
    <div class="chart-area">
      <div class="legend">
        <span><i style="background:var(--ma5)"></i>MA5</span>
        <span><i style="background:var(--ma20)"></i>MA20</span>
        <span><i style="background:var(--ma60)"></i>MA60</span>
      </div>
      <canvas id="chart"></canvas>
    </div>
  </div>
</main>
<script>
const DATA = __DATA__;
const HIGH=70, LOW=30, CROSS=80;
let curState="", curCode=null, sortKey="state", sortAsc=true;

document.getElementById('mDate').textContent = DATA.date;
document.getElementById('mCount').textContent = DATA.rows.length;

const order=["③加速冲顶","②稳健上行","①领涨减速","⑥弱转强","⑨底背离","⑤中性震荡","④强转弱","⑧下跌中继","⑦持续杀跌"];
const cnt={}; DATA.rows.forEach(r=>cnt[r.state]=(cnt[r.state]||0)+1);
const distEl=document.getElementById('dist');
order.forEach(s=>{ if(!cnt[s])return;
  const c=document.createElement('div'); c.className='chip'; c.dataset.state=s;
  c.innerHTML=s+'<span class="n">'+cnt[s]+'</span>';
  c.onclick=()=>{ curState=(curState===s?"":s); renderChips(); render(); };
  distEl.appendChild(c);
});
function renderChips(){ [...distEl.children].forEach(c=>c.classList.toggle('active',c.dataset.state===curState)); }

function trendCls(t){ return t==='上涨'?'tg-up':t==='下跌'?'tg-down':'tg-flat'; }
function trendBadge(r){
  if(r.trend!=='横盘'||!r.trend_badge) return '';
  const b=parseInt(r.trend_badge,10);
  if(b===0) return '';
  if(b<0) return '<span class="badge-neg" title="下穿20日线 '+Math.abs(b)+' 天（空方破位）">'+b+'</span>';
  return '<span class="badge-pos" title="上穿20日线 '+b+' 天（多方破位）">+'+b+'</span>';
}
function stColor(s){ const h=s[0];
  if(h==='③'||h==='②'||h==='⑥'||h==='⑨') return '#f87171';
  if(h==='①') return '#fbbf24';
  if(h==='⑤') return '#c9d1d9';
  return '#4ade80';
}
function pctCell(v){ if(v==null)return '<span class="scode">—</span>';
  const col=v>=70?'var(--up)':v<=30?'var(--down)':'var(--accent)';
  return v.toFixed(0)+'<span class="pctbar"><i style="width:'+Math.max(2,Math.min(100,v))+'%;background:'+col+'"></i></span>'; }

const tbody=document.getElementById('tbody');
function render(){
  const q=document.getElementById('search').value.trim().toLowerCase();
  const ft=document.getElementById('fTrend').value;
  let list=DATA.rows.filter(r=>{
    if(curState && r.state!==curState) return false;
    if(ft && r.trend!==ft) return false;
    if(q && !(r.name.toLowerCase().includes(q)||r.code.toLowerCase().includes(q))) return false;
    return true;
  });
  const sk=sortKey;
  list.sort((a,b)=>{
    let va=a[sk], vb=b[sk];
    if(sk==='state'){ va=order.indexOf(a.state); vb=order.indexOf(b.state); }
    if(sk==='name'){ return sortAsc? a.name.localeCompare(b.name,'zh'):b.name.localeCompare(a.name,'zh'); }
    if(sk==='trend'){ const o={'上涨':0,'横盘':1,'下跌':2}; va=o[a.trend]; vb=o[b.trend]; }
    va=(va==null?-1:va); vb=(vb==null?-1:vb);
    return sortAsc? va-vb : vb-va;
  });
  tbody.innerHTML='';
  list.forEach(r=>{
    const tr=document.createElement('tr');
    if(r.code===curCode) tr.className='sel';
    tr.innerHTML=
      '<td class="l"><div class="sname">'+r.name+'</div><div class="scode">'+r.code+'</div></td>'+
      '<td><span class="tg '+trendCls(r.trend)+'">'+r.trend+'</span>'+trendBadge(r)+'</td>'+
      '<td><span class="st" style="color:'+stColor(r.state)+'">'+r.state+'</span></td>'+
      '<td>'+pctCell(r.rs_pct)+'</td>'+
      '<td>'+pctCell(r.rs_mom_pct)+'</td>'+
      '<td>'+(r.cross==null?'<span class="scode">—</span>':r.cross.toFixed(0))+'</td>';
    tr.onclick=()=>{ curCode=r.code; render(); showDetail(r); };
    tbody.appendChild(tr);
  });
}

document.querySelectorAll('thead th').forEach(th=>{
  th.onclick=()=>{ const k=th.dataset.k; if(sortKey===k) sortAsc=!sortAsc; else{sortKey=k; sortAsc=(k==='name');} render(); };
});
document.getElementById('search').oninput=render;
document.getElementById('fTrend').onchange=render;

function rsDir(p){ if(p==null)return['走平','gate-na']; if(p>HIGH)return['增强','gate-ok']; if(p<LOW)return['减弱','gate-no']; return['走平','gate-na']; }
function showDetail(r){
  const [dir,dcls]=rsDir(r.rs_mom_pct);
  const base={'增强上涨':'③加速冲顶','走平上涨':'②稳健上行','减弱上涨':'①领涨减速',
    '增强横盘':'⑥弱转强','走平横盘':'⑤中性震荡','减弱横盘':'④强转弱',
    '增强下跌':'⑨底背离','走平下跌':'⑧下跌中继','减弱下跌':'⑦持续杀跌'}[dir+r.trend];
  let gates='';
  if(dir==='增强' && (r.trend==='上涨'||r.trend==='横盘')){
    if(r.trend==='上涨'){
      const crossOk=r.cross!=null && r.cross>CROSS;
      gates+='<span class="gate '+(crossOk?'gate-ok':'gate-no')+'">横截面领跑 '+(r.cross==null?'无数据':r.cross.toFixed(0)+(crossOk?' > 80 通过':' <= 80 未通过'))+'</span>';
    }
    if(base!==r.state){
      gates+='<span class="gate gate-no">闸门降级：'+base+' -> '+r.state+'</span>';
    } else if(r.trend==='上涨' && r.state==='③加速冲顶'){
      gates+='<span class="gate gate-ok">通过全部闸门 -> ③成立</span>';
    }
  }
  const el=document.getElementById('verdict');
  el.innerHTML=
    '<div class="row1"><h2>'+r.name+' <span class="scode">'+r.code+'</span></h2>'+
      '<span class="st" style="font-size:15px;color:'+stColor(r.state)+'">'+r.state+'</span>'+
      '<span class="tg '+trendCls(r.trend)+'">趋势：'+r.trend+'</span>'+trendBadge(r)+'</div>'+
    '<div class="why">'+
      '价格趋势：<b>'+r.trend+'</b>'+(r.trend==='横盘'&&r.trend_badge?trendBadge(r):'')+'（上穿60=上涨 / 击穿60=下跌 / 死叉·金叉=横盘并标注穿越天数）｜ '+
      'RS动量分位：<b>'+(r.rs_mom_pct==null?'—':r.rs_mom_pct.toFixed(0))+'</b> -> RS方向 <span class="gate '+dcls+'">'+dir+'</span>｜ '+
      'RS水平分位：<b>'+(r.rs_pct==null?'—':r.rs_pct.toFixed(0))+'</b>｜ '+
      '横截面动量：<b>'+(r.cross==null?'—':r.cross.toFixed(0))+'</b><br>'+
      '九宫格基础映射：<b>'+dir+' x '+r.trend+' -> '+base+'</b> '+gates+
    '</div>';
  drawChart(r.code);
}

const cv=document.getElementById('chart'), ctx=cv.getContext('2d');
let hoverIdx=-1, curK=null;
function drawChart(code){
  curK=DATA.kline[code]; if(!curK){return;}
  const dpr=window.devicePixelRatio||1;
  const rect=cv.parentElement.getBoundingClientRect();
  const W=rect.width-28, H=rect.height-24;
  cv.width=W*dpr; cv.height=H*dpr; cv.style.width=W+'px'; cv.style.height=H+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  const k=curK, n=k.c.length;
  const padL=54,padR=12,padT=14,padB=22;
  const cw=W-padL-padR, ch=H-padT-padB;
  let lo=Infinity,hi=-Infinity;
  for(let i=0;i<n;i++){ const l=k.l.length?k.l[i]:k.c[i]; const h=k.h.length?k.h[i]:k.c[i];
    lo=Math.min(lo,l,k.ma60[i]); hi=Math.max(hi,h,k.ma60[i]); }
  const pad=(hi-lo)*0.05; lo-=pad; hi+=pad;
  const x=i=>padL+cw*(i+0.5)/n;
  const y=v=>padT+ch*(1-(v-lo)/(hi-lo));
  ctx.font='10px sans-serif'; ctx.textBaseline='middle';
  for(let g=0;g<=4;g++){ const val=lo+(hi-lo)*g/4; const yy=y(val);
    ctx.strokeStyle='#1c222b'; ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.fillStyle='#8b949e'; ctx.textAlign='right'; ctx.fillText(val.toFixed(1),padL-6,yy); }
  ctx.textAlign='center';
  const ticks=6;
  for(let t=0;t<=ticks;t++){ const i=Math.min(n-1,Math.round((n-1)*t/ticks));
    ctx.fillStyle='#8b949e'; ctx.fillText(k.d[i].slice(2),x(i),H-8); }
  const bw=Math.max(1,Math.min(7,cw/n*0.62));
  for(let i=0;i<n;i++){
    const o=k.o.length?k.o[i]:k.c[i], c=k.c[i];
    const h=k.h.length?k.h[i]:Math.max(o,c), l=k.l.length?k.l[i]:Math.min(o,c);
    const up=c>=o; const col=up?'#ef4444':'#22c55e';
    ctx.strokeStyle=col; ctx.fillStyle=col;
    ctx.beginPath(); ctx.moveTo(x(i),y(h)); ctx.lineTo(x(i),y(l)); ctx.stroke();
    const yo=y(o),yc=y(c); const top=Math.min(yo,yc); let bh=Math.abs(yc-yo); if(bh<1)bh=1;
    ctx.fillRect(x(i)-bw/2,top,bw,bh);
  }
  function line(arr,col){ ctx.strokeStyle=col; ctx.lineWidth=1.2; ctx.beginPath();
    for(let i=0;i<n;i++){ const yy=y(arr[i]); if(i===0)ctx.moveTo(x(i),yy); else ctx.lineTo(x(i),yy);} ctx.stroke(); ctx.lineWidth=1; }
  line(k.ma5,'#3b82f6'); line(k.ma20,'#a855f7'); line(k.ma60,'#f59e0b');
  if(hoverIdx>=0 && hoverIdx<n){
    const i=hoverIdx; ctx.strokeStyle='#4b5563'; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(x(i),padT); ctx.lineTo(x(i),H-padB); ctx.stroke(); ctx.setLineDash([]);
    const o=k.o.length?k.o[i]:k.c[i], c=k.c[i];
    const h=k.h.length?k.h[i]:Math.max(o,c), l=k.l.length?k.l[i]:Math.min(o,c);
    const chg=i>0?((c/k.c[i-1]-1)*100):0;
    const txt=k.d[i]+'  开'+o.toFixed(1)+' 高'+h.toFixed(1)+' 低'+l.toFixed(1)+' 收'+c.toFixed(1)+' ('+(chg>=0?'+':'')+chg.toFixed(2)+'%)';
    ctx.font='11px sans-serif'; const tw=ctx.measureText(txt).width;
    let bx=x(i)+8; if(bx+tw+12>W) bx=x(i)-tw-16;
    ctx.fillStyle='rgba(13,17,23,.92)'; ctx.fillRect(bx,padT+2,tw+12,20);
    ctx.strokeStyle='#30363d'; ctx.strokeRect(bx,padT+2,tw+12,20);
    ctx.fillStyle=chg>=0?'#f87171':'#4ade80'; ctx.textAlign='left'; ctx.textBaseline='middle';
    ctx.fillText(txt,bx+6,padT+12);
  }
}
cv.onmousemove=e=>{ if(!curK)return; const rect=cv.getBoundingClientRect();
  const W=rect.width; const padL=54,padR=12; const cw=W-padL-padR; const n=curK.c.length;
  const mx=e.clientX-rect.left; let i=Math.floor((mx-padL)/cw*n); i=Math.max(0,Math.min(n-1,i));
  if(i!==hoverIdx){ hoverIdx=i; drawChart(curCode);} };
cv.onmouseleave=()=>{ hoverIdx=-1; if(curCode)drawChart(curCode); };
window.addEventListener('resize',()=>{ if(curCode)drawChart(curCode); });

render();
</script>
</body>
</html>"""

out = HTML.replace("__DATA__", data_str)
with open("industry_state_review.html", "w", encoding="utf-8") as f:
    f.write(out)
print("HTML generated: industry_state_review.html  size %d KB" % (len(out) // 1024))
