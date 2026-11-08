// Shared by Denial Watch (js/denial_watch.js) and its open enrollment page
// (js/open_enrollment.js), and loaded before either: the opener for folded
// sections, the number formats, the colour tokens and the line chart. Plain
// script, no build step.
window.DWCharts=(function(){
/* Sources and the enrollment questions start closed: a link to anything inside opens its <details> first, so the jump lands. */
const openTo=hash=>{let el=null,shut=false;
  try{el=hash&&hash.length>1?document.getElementById(decodeURIComponent(hash.slice(1))):null;}catch(e){}
  for(let d=el;d;d=d.parentElement)if(d.tagName==='DETAILS'&&!d.open){d.open=true;shut=true;}
  return shut?el:null;};
const landOn=()=>{const el=openTo(location.hash);if(el)el.scrollIntoView();};
/* A page calls this before it draws, so the landing waits for its charts and the tiles above don't move it; a click opens before the browser jumps. */
function foldLinks(){
setTimeout(landOn,0);window.addEventListener('hashchange',landOn);
document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a[href^="#"]');if(a)openTo(a.getAttribute('href'));});
}
const fmt=n=>Math.round(n).toLocaleString('en-US');
const pct=(x,d=1)=>(x*100).toFixed(d)+'%';
/* A colour token from templates/partials/denial_watch_style.html, the one place chart colours are written. */
const tok=v=>{const r=document.querySelector('.dw');return r?getComputedStyle(r).getPropertyValue(v).trim():'';};

/* line chart */
/* ink: the end label's colour, when the line's own is too light for words. */
/* also: more lines on the same axes, drawn under the main one; dash makes a line dashed, so it reads without colour. */
function line(svg,pts,{ymin,ymax,fmtY,ticks,color,ink=color,label,open,dash,also=[]}){
  const paper=tok('--dw-paper');
  const W=560,H=270,L=46,R=16,Tp=18,B=34;const xs=pts.map(p=>p.x);const x0=Math.min(...xs),x1=Math.max(...xs);
  const X=x=>L+(x-x0)/(x1-x0||1)*(W-L-R), Y=y=>Tp+(1-(y-ymin)/(ymax-ymin))*(H-Tp-B);
  let h='<g class="dw-axis">';for(const t of ticks){h+=`<line x1="${L}" x2="${W-R}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${L-8}" y="${Y(t)+4}" text-anchor="end" font-size="12">${fmtY(t)}</text>`;}h+='</g>';
  /* Every step-th year and the last, leaving out a step year that would crowd the last. */
  const step=Math.ceil(pts.length/8),last=pts.length-1;pts.forEach((p,i)=>{if(i===last||(i%step===0&&last-i>=step))h+=`<text x="${X(p.x)}" y="${H-10}" text-anchor="middle" font-size="12">${p.x}</text>`;});
  const series=(pts,{color,ink=color,label,open,dash})=>{const last=pts.length-1;
    h+=`<path d="${pts.map((p,i)=>(i?'L':'M')+X(p.x).toFixed(1)+' '+Y(p.y).toFixed(1)).join(' ')}" fill="none" stroke="${color}" stroke-width="3" stroke-linejoin="round"${dash?` stroke-dasharray="${dash}"`:''}/>`;
    /* A point that isn't final yet (open) is drawn hollow. */
    pts.forEach((p,i)=>{const o=open&&open(p);h+=`<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="${i===pts.length-1?5:3.5}" fill="${o?paper:color}" stroke="${o?color:paper}" stroke-width="${o?2.5:1.5}"/>`;});
    /* The end label goes under the last point when the line comes down to it, so it doesn't sit on the line. */
    const lp=pts[last],below=last>0&&pts[last-1].y>lp.y&&Y(lp.y)+26<H-B;h+=`<text class="dw-end" x="${X(lp.x)-8}" y="${Y(lp.y)+(below?26:-12)}" text-anchor="end" font-size="14" font-weight="700" style="fill:${ink}">${label(lp.y)}</text>`;};
  /* Each line in its own group, the main one last, so the motion below can reveal or move it. */
  for(const s of also){h+='<g class="dw-line">';series(s.pts,s);h+='</g>';}
  h+='<g class="dw-line dw-line-main">';series(pts,{color,ink,label,open,dash});h+='</g>';
  svg.innerHTML=h;
  if(!MOTION)return;
  const prev=svg._dwY;svg._dwY=new Map(pts.map(p=>[p.x,Y(p.y)]));
  const pp=pts.map(p=>[X(p.x),Y(p.y),prev&&prev.has(p.x)?prev.get(p.x):H-B]);
  /* The same view picked again stays still. */
  if(prev&&seen.has(svg)){if(pp.some(([,y,y0])=>Math.abs(y-y0)>0.01))glide(svg,pp);}
  else onView(svg,()=>hideLines(svg),()=>drawIn(svg));
}

/* Motion: each chart moves once as it comes into view, then stays still. Nothing moves for anyone who asks for less motion, and print shows the finished chart. */
const MQ=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: no-preference)');
let MOTION=!!(MQ&&MQ.matches&&'IntersectionObserver' in window&&Element.prototype.animate);
/* Frame-by-frame steps (a glide, a count) register how to finish at once. */
const finishers=new Set();
/* Finish everything now and stop: every chart at its final state, nothing waiting. For printing, and for turning on reduced motion mid-visit. */
function settle(){MOTION=false;if(io)io.disconnect();finishers.forEach(f=>f());finishers.clear();document.getAnimations().forEach(a=>{if(a.effect&&a.effect.target&&a.effect.target.closest&&a.effect.target.closest('.dw'))a.finish();});
  document.querySelectorAll('.dw .dw-bar-fill,.dw .dw-dumb i').forEach(e=>{e.style.scale='';e.style.translate='';});
  document.querySelectorAll('.dw .dw-line').forEach(g=>g.style.clipPath='');document.querySelectorAll('.dw .dw-end').forEach(t=>t.style.opacity='');}
window.addEventListener('beforeprint',settle);
if(MQ&&MQ.addEventListener)MQ.addEventListener('change',()=>{if(!MQ.matches)settle();});
const EASE='cubic-bezier(.2,.7,.2,1)';
const seen=new WeakSet();
const inView=el=>{const r=el.getBoundingClientRect();return r.height>0&&r.bottom>0&&r.top<window.innerHeight;};
const io=MOTION?new IntersectionObserver(es=>es.forEach(e=>{if(!e.isIntersecting)return;io.unobserve(e.target);seen.add(e.target);const p=e.target._dwPlay;e.target._dwPlay=null;if(p)p();}),{rootMargin:'0px 0px -15% 0px'}):null;
/* hold sets the start state for a chart below the fold; play runs when it scrolls in, or now if it's already showing. */
function onView(el,hold,play){if(!MOTION||!el)return;if(seen.has(el)||inView(el)){seen.add(el);play();return;}hold();el._dwPlay=play;io.observe(el);}

/* A line draws left to right, its dots appearing as it reaches them, then the end label. */
const HIDE='inset(0 100% 0 0)';
function hideLines(svg){svg.querySelectorAll('.dw-line').forEach(g=>g.style.clipPath=HIDE);svg.querySelectorAll('.dw-end').forEach(t=>t.style.opacity='0');}
function drawIn(svg){
  svg.querySelectorAll('.dw-line').forEach(g=>{g.style.clipPath='';g.animate({clipPath:[HIDE,'inset(0 0% 0 0)']},{duration:1100,easing:'cubic-bezier(.4,0,.2,1)',fill:'backwards'});});
  svg.querySelectorAll('.dw-end').forEach(t=>{t.style.opacity='';t.animate({opacity:[0,1]},{duration:300,delay:1000,fill:'backwards'});});}
/* Switching views glides the main line from its old heights to its new ones; a year the old view lacked rises from the axis. The final attributes are set exactly at the end. */
function glide(svg,pp){if(svg._dwDone)svg._dwDone();const g=svg.querySelector('.dw-line-main');if(!g)return;
  const path=g.querySelector('path'),dots=[...g.querySelectorAll('circle')],end=g.querySelector('.dw-end'),d=path.getAttribute('d'),cy=dots.map(c=>c.getAttribute('cy')),t0=performance.now();
  const done=()=>{cancelAnimationFrame(svg._dwRaf);finishers.delete(done);path.setAttribute('d',d);dots.forEach((c,i)=>c.setAttribute('cy',cy[i]));};finishers.add(done);svg._dwDone=done;
  const run=now=>{const p=Math.min(1,(now-t0)/600),e=p<.5?4*p*p*p:1-Math.pow(-2*p+2,3)/2;
    if(p>=1){done();return;}
    const ys=pp.map(([,y,y0])=>y0+(y-y0)*e);
    path.setAttribute('d',pp.map(([x],i)=>(i?'L':'M')+x.toFixed(1)+' '+ys[i].toFixed(1)).join(' '));dots.forEach((c,i)=>c.setAttribute('cy',ys[i]));
    svg._dwRaf=requestAnimationFrame(run);};
  if(end)end.animate({opacity:[0,1]},{duration:300,delay:400,fill:'backwards'});
  run(t0);}

/* Bars grow from the left, one just after another. A redraw of the same view (a hover, a pick) stays still; a new view grows again. */
function grow(box,key){if(!MOTION||!box)return;if(box._dwKey===key&&seen.has(box))return;box._dwKey=key;
  const fills=[...box.querySelectorAll('.dw-bar-fill')],gap=Math.min(45,500/(fills.length||1));
  onView(box,()=>fills.forEach(f=>f.style.scale='0 1'),()=>fills.forEach((f,i)=>{f.style.scale='';f.animate({scale:['0 1','1 1']},{duration:650,delay:i*gap,easing:EASE,fill:'backwards'});}));}

/* A change's later dot starts on the earlier one and slides to where it ends, its line drawn behind it, so the motion shows the direction. */
function slide(box){if(!MOTION||!box)return;const ds=[...box.querySelectorAll('.dw-dumb')];
  const parts=s=>{const a=parseFloat(s.querySelector('.dw-dumb-from').style.left),b=parseFloat(s.querySelector('.dw-dumb-to').style.left);return {to:s.querySelector('.dw-dumb-to'),ln:s.querySelector('.dw-dumb-line'),a,b,dx:(a-b)/100*s.clientWidth+'px 0'};};
  onView(box,()=>ds.forEach(s=>{const p=parts(s);p.to.style.translate=p.dx;p.ln.style.scale='0 1';}),
    ()=>ds.forEach((s,i)=>{const p=parts(s),o={duration:900,delay:200+i*90,easing:EASE,fill:'backwards'};
      p.to.style.translate='';p.ln.style.scale='';p.ln.style.transformOrigin=p.b<p.a?'right center':'left center';
      p.to.animate({translate:[p.dx,'0 0']},o);p.ln.animate({scale:['0 1','1 1']},o);}));}

/* Headline numbers count up to the figure, only where they show when the page opens, so a tile below the fold never reads 0. */
function countUp(box){if(!MOTION||!box||!inView(box))return;seen.add(box);
  box.querySelectorAll('.dw-stat-head b').forEach((b,i)=>{const t=b.textContent,m=t.match(/^(\D*)(\d[\d,]*(?:\.\d+)?)(.*)$/s);if(!m)return;
    const v=parseFloat(m[2].replace(/,/g,'')),dp=(m[2].split('.')[1]||'').length,comma=m[2].includes(','),
      f=x=>m[1]+(comma?x.toLocaleString('en-US',{minimumFractionDigits:dp,maximumFractionDigits:dp}):x.toFixed(dp))+m[3],t0=performance.now()+i*120;
    let raf=0;const done=()=>{cancelAnimationFrame(raf);finishers.delete(done);b.textContent=t;};finishers.add(done);
    const run=now=>{const p=Math.min(1,Math.max(0,(now-t0)/1000));if(p>=1){done();return;}b.textContent=f(v*(1-Math.pow(1-p,3)));raf=requestAnimationFrame(run);};
    run(t0-1);});}

return {foldLinks,fmt,pct,tok,line,grow,slide,countUp};
})();
