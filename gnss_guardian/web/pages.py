"""The guidance page served by live mode at /guide: for the person walking the rig, on a phone.

A big arrow: which way to walk relative to the rig's current heading (the camera's forward axis,
which is the push direction when the camera is mounted image-top forward), toward the next keyframe
waypoint on the way home (or home itself). Distance, state, a full-screen HOME REACHED, a red
DEMO INJECTION ACTIVE banner, and STALE when live.json stops changing for more than 3 s. Updates
once a second. With --demo-inject the demo controls appear at the bottom.
"""

GUIDE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Guardian guide</title>
<style>
:root{--bg:#0b1220;--fg:#e8eef8;--dim:#8a97ad;--ok:#3ccf6e;--warn:#ffb020;--bad:#ff4d4f;--blue:#4aa3ff;--card:#141e33}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{display:flex;flex-direction:column;min-height:100vh;padding:12px 16px env(safe-area-inset-bottom)}
#state{font-weight:800;font-size:clamp(22px,7vw,40px);letter-spacing:.02em;text-align:center;padding:10px;border-radius:12px;background:var(--card)}
#sub{text-align:center;color:var(--dim);font-size:15px;margin:6px 0 0;min-height:1.3em}
#inj{display:none;background:var(--bad);color:#fff;font-weight:800;text-align:center;padding:10px;border-radius:10px;margin-top:10px;font-size:18px}
#stale{display:none;background:#555;color:#fff;font-weight:700;text-align:center;padding:10px;border-radius:10px;margin-top:10px}
#main{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px}
#arrow{width:min(70vw,52vh);height:min(70vw,52vh);transition:transform .6s ease}
#dist{font-size:clamp(48px,18vw,110px);font-weight:800;line-height:1}
#what{color:var(--dim);font-size:17px;text-align:center}
#note{color:var(--warn);font-size:14px;text-align:center;min-height:1.2em}
#home{display:none;position:fixed;inset:0;background:var(--ok);color:#04210f;flex-direction:column;align-items:center;justify-content:center;text-align:center;z-index:5}
#home h1{font-size:clamp(44px,15vw,96px);margin:0}#home p{font-size:20px;margin:8px 16px}
#demo{display:none;background:var(--card);border-radius:12px;padding:10px;margin-top:10px}
#demo .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
#demo input{width:72px;font-size:16px;padding:6px;border-radius:6px;border:1px solid #334;background:#0b1220;color:var(--fg)}
#demo button{font-size:16px;padding:8px 12px;border-radius:8px;border:0;background:var(--blue);color:#fff;font-weight:700}
#demo button.stop{background:var(--bad)}#demo small{color:var(--dim)}
</style></head><body>
<div id="state">CONNECTING</div><div id="sub"></div>
<div id="inj">DEMO INJECTION ACTIVE &mdash; not a real attack</div>
<div id="stale">STALE: no update for <span id="age">?</span> s &mdash; GNSS state unknown</div>
<div id="main">
  <svg id="arrow" viewBox="-50 -50 100 100" aria-label="direction to walk">
    <circle r="47" fill="none" stroke="#24314d" stroke-width="2"/>
    <path id="arrowpath" d="M0,-42 L22,4 L8,0 L8,38 L-8,38 L-8,0 L-22,4 Z" fill="#4aa3ff"/>
  </svg>
  <div id="dist">&ndash;</div><div id="what"></div><div id="note"></div>
</div>
<div id="demo">
  <div><b>Demo injection</b> <small>software only, real signal strengths; detector is not told</small></div>
  <div class="row">jump <input id="jm" type="number" value="30" min="1"> m at <input id="jb" type="number" value="90"> deg
    <button onclick="inject('jump')">Jump</button></div>
  <div class="row">drift <input id="dv" type="number" value="0.5" step="0.1" min="0.1"> m/s at <input id="db" type="number" value="90"> deg
    <button onclick="inject('drift')">Drift</button></div>
  <div class="row"><button class="stop" onclick="inject('stop')">Stop injection</button> <small id="injmsg"></small></div>
</div>
<div id="home"><h1>HOME REACHED</h1><p id="homep">home keyframe matched</p></div>
<script>
const COL={GNSS_OK:'#3ccf6e',WARMING_UP:'#8a97ad',SUSPECT:'#ffb020',JAMMED:'#ff6a3d',SPOOFED:'#ff4d4f',
  VISION_NAV:'#4aa3ff',RETURN_HOME:'#4aa3ff',HOME_REACHED:'#3ccf6e'};
let lastSeq=null,lastChange=Date.now(),rot=0;
const $=id=>document.getElementById(id);
function turn(target){let d=((target-rot)%360+540)%360-180;rot+=d;$('arrow').style.transform=`rotate(${rot}deg)`}
function fmtd(m){return m==null?'–':(m<10?m.toFixed(1):m.toFixed(0))+' m'}
async function poll(){
  try{
    const r=await fetch('/live.json',{cache:'no-store'});
    if(!r.ok)throw new Error(r.status);
    const v=await r.json();
    if(v.seq!==lastSeq){lastSeq=v.seq;lastChange=Date.now()}
    show(v);
  }catch(e){$('state').textContent='NO CONNECTION';$('state').style.color='#ff4d4f'}
  const age=(Date.now()-lastChange)/1000;
  $('stale').style.display=age>3?'block':'none';$('age').textContent=age.toFixed(0);
}
function show(v){
  const st=v.state||'WAITING',g=v.guidance,fu=v.fused,vis=v.vision||{};
  const stale=(Date.now()-lastChange)/1000>3;
  $('state').textContent=stale?'GNSS UNKNOWN':st.replace('_',' ');
  $('state').style.color=stale?'#aaa':(COL[st]||'#e8eef8');
  $('sub').textContent=(v.labels&&v.labels.length?v.labels.join(' + '):(v.gnss_trusted?'GNSS trusted':'GNSS not trusted'))
    +(fu?` · position from ${fu.source.replace('_',' ')}`:'');
  const inj=v.injection&&v.injection.active;
  $('inj').style.display=inj?'block':'none';
  if(inj)$('inj').innerHTML=`DEMO INJECTION ACTIVE &mdash; ${inj.type} ${inj.type==='jump'?inj.m+' m':inj.mps+' m/s'} at ${inj.bearing_deg}&deg; (${inj.elapsed_s} s) &mdash; not a real attack`;
  $('demo').style.display=v.injection&&v.injection.enabled?'block':'none';
  $('home').style.display=st==='HOME_REACHED'&&!stale?'flex':'none';
  if(st==='HOME_REACHED'&&vis.home_confirmed)$('homep').textContent=
    `home keyframe matched ${vis.home_confirmed.offset_m} m away (${vis.home_confirmed.inliers} inliers)`;
  const aligned=vis.aligned, hdg=vis.heading;
  let bearing=null,dist=null,what='',note='';
  if(g){const w=g.waypoint;bearing=w.bearing_deg;dist=w.distance_m;
    what=w.id==='home'?'to HOME':`to waypoint ${w.id} · home ${fmtd(g.home_distance_m)} · ${g.waypoints_left} left`;}
  else if(fu&&v.home&&v.home.set){bearing=fu.home_bearing_deg;dist=fu.home_distance_m;what='home is';
    note=st==='GNSS_OK'?'GNSS trusted: walk out, guidance starts if GNSS is lost or spoofed':'';}
  else{what=v.home&&!v.home.set?v.home.detail:'waiting';}
  $('arrowpath').setAttribute('fill',g?'#4aa3ff':'#56627a');
  if(bearing!=null){
    if(aligned&&hdg!=null){turn(bearing-hdg)}else{turn(bearing);note='heading not aligned yet: arrow is a compass bearing (top = north)'}
  }
  $('dist').textContent=fmtd(dist);$('what').textContent=what;$('note').textContent=note;
}
async function inject(kind){
  let q='';
  if(kind==='jump')q=`?m=${$('jm').value}&bearing=${$('jb').value}`;
  if(kind==='drift')q=`?mps=${$('dv').value}&bearing=${$('db').value}`;
  try{const r=await fetch('/inject/'+kind+q,{method:'POST'});const j=await r.json();
    $('injmsg').textContent=r.ok?(kind==='stop'?'stopped':'started'):(j.error||r.status)}
  catch(e){$('injmsg').textContent='failed: '+e}
}
poll();setInterval(poll,1000);
</script></body></html>
"""
