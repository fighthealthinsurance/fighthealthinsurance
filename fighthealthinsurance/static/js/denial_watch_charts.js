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
function line(svg,pts,{ymin,ymax,fmtY,ticks,color,ink=color,label,open}){
  const paper=tok('--dw-paper');
  const W=560,H=270,L=46,R=16,Tp=18,B=34;const xs=pts.map(p=>p.x);const x0=Math.min(...xs),x1=Math.max(...xs);
  const X=x=>L+(x-x0)/(x1-x0||1)*(W-L-R), Y=y=>Tp+(1-(y-ymin)/(ymax-ymin))*(H-Tp-B);
  let h='<g class="dw-axis">';for(const t of ticks){h+=`<line x1="${L}" x2="${W-R}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${L-8}" y="${Y(t)+4}" text-anchor="end" font-size="12">${fmtY(t)}</text>`;}h+='</g>';
  /* Every step-th year and the last, leaving out a step year that would crowd the last. */
  const step=Math.ceil(pts.length/8),last=pts.length-1;pts.forEach((p,i)=>{if(i===last||(i%step===0&&last-i>=step))h+=`<text x="${X(p.x)}" y="${H-10}" text-anchor="middle" font-size="12">${p.x}</text>`;});
  h+=`<path d="${pts.map((p,i)=>(i?'L':'M')+X(p.x).toFixed(1)+' '+Y(p.y).toFixed(1)).join(' ')}" fill="none" stroke="${color}" stroke-width="3" stroke-linejoin="round"/>`;
  /* A point that isn't final yet (open) is drawn hollow. */
  pts.forEach((p,i)=>{const o=open&&open(p);h+=`<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="${i===pts.length-1?5:3.5}" fill="${o?paper:color}" stroke="${o?color:paper}" stroke-width="${o?2.5:1.5}"/>`;});
  /* The end label goes under the last point when the line comes down to it, so it doesn't sit on the line. */
  const lp=pts[last],below=last>0&&pts[last-1].y>lp.y&&Y(lp.y)+26<H-B;h+=`<text class="dw-end" x="${X(lp.x)-8}" y="${Y(lp.y)+(below?26:-12)}" text-anchor="end" font-size="14" font-weight="700" style="fill:${ink}">${label(lp.y)}</text>`;
  svg.innerHTML=h;
}

return {foldLinks,fmt,pct,tok,line};
})();
